import os
import mysql.connector
from dotenv import load_dotenv 
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()


app = Flask(__name__)
app.config["SECRET_KEY"]=os.getenv("SECRET_KEY")
print("Secret key loaded:",bool(app.config["SECRET_KEY"]))

app.secret_key= os.getenv("SECRET_KEY")
def get_db_connection():
    conn = mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
        )
    return conn
@app.context_processor
def inject_theme():
    return {
        "theme_preference": session.get("theme_preference", "light")
    }
@app.before_request
def apply_overdue_fines():

    if request.endpoint == "static":
        return

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            UPDATE issued_books
            SET fine_amount = 100
            WHERE due_date < CURDATE()
            AND return_date IS NULL
            AND fine_amount = 0
        """)

        conn.commit()

    finally:
        cursor.close()
        conn.close()


@app.before_request
def update_student_activity():

    if "student_id" not in session:
        return

    if request.endpoint == "static":
        return

    conn = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE student_sessions
            SET last_seen = NOW()
            WHERE member_id = %s
        """, (session["student_id"],))

        conn.commit()

    except mysql.connector.Error:
        pass

    finally:
        if cursor:
            cursor.close()

        if conn:
            conn.close()



@app.route("/")
def home():
    if not session.get("librarian_logged_in"):
        return redirect(url_for("librarian_login"))

    conn = get_db_connection()
    cursor = conn.cursor()

    # Total book copies
    cursor.execute("SELECT COALESCE(SUM(quantity), 0) FROM books")
    total_books = cursor.fetchone()[0]

    # Issued books
    cursor.execute("""
        SELECT COUNT(*)
        FROM issued_books
        WHERE return_date IS NULL
    """)
    issued_count = cursor.fetchone()[0]

    # Available books
    available_books = total_books - issued_count

    # Overdue books
    cursor.execute("""
        SELECT COUNT(*)
        FROM issued_books
        WHERE return_date IS NULL
        AND due_date < CURDATE()
    """)
    overdue_count = cursor.fetchone()[0]

    # Total registered students
    cursor.execute("SELECT COUNT(*) FROM members")
    total_students = cursor.fetchone()[0]

    # Active students
    cursor.execute("""
        SELECT COUNT(*)
        FROM student_sessions
        WHERE last_seen >= NOW() - INTERVAL 15 MINUTE
    """)
    active_students = cursor.fetchone()[0]

    # Fetch latest 4 books
    cursor.execute("""
        SELECT book_id, title, author, category, quantity
        FROM books
        ORDER BY book_id DESC
        LIMIT 4
    """)
    recent_books = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "index.html",
        total_books=total_books,
        available_books=available_books,
        issued_count=issued_count,
        overdue_count=overdue_count,
        total_students=total_students,
        active_students=active_students,
        recent_books=recent_books
    )


@app.route("/students", methods=["GET", "POST"])
def students():
    conn = get_db_connection()
    cursor = conn.cursor()

    message = None
    message_type = None

    if request.args.get("delete_error") == "history":
        message = "Cannot delete this student because borrowing history exists."
        message_type = "error"

    try:
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip()
            phone = request.form.get("phone", "").strip()

            cursor.execute(
                "INSERT INTO members (name, email, phone) VALUES (%s, %s, %s)",
                (name, email, phone)
            )

            conn.commit()
            message = "Student registered successfully!"
            message_type = "success"

        cursor.execute("""
            SELECT member_id, name, email, phone
            FROM members
            ORDER BY member_id DESC
        """)

        students = cursor.fetchall()

    except mysql.connector.Error as err:
        conn.rollback()
        message = f"Database error: {err}"
        message_type = "error"
        students = []

    finally:
        cursor.close()
        conn.close()

    return render_template(
        "students.html",
        students=students,
        message=message,
        message_type=message_type
    )
@app.route("/edit-student/<int:member_id>", methods=["GET", "POST"])
def edit_student(member_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip()
            phone = request.form.get("phone", "").strip()

            cursor.execute("""
                UPDATE members
                SET name = %s, email = %s, phone = %s
                WHERE member_id = %s
            """, (name, email, phone, member_id))

            conn.commit()
            return redirect(url_for("students"))

        cursor.execute("""
            SELECT member_id, name, email, phone
            FROM members
            WHERE member_id = %s
        """, (member_id,))

        student = cursor.fetchone()

        if student is None:
            return redirect(url_for("students"))

        return render_template("edit_student.html", student=student)

    except mysql.connector.Error as err:
        conn.rollback()
        return f"Database error: {err}"

    finally:
        cursor.close()
        conn.close()

@app.route("/delete-student/<int:member_id>", methods=["POST"])
def delete_student(member_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Check whether borrowing history exists
        cursor.execute("""
            SELECT COUNT(*)
            FROM issued_books
            WHERE member_id = %s
        """, (member_id,))

        count = cursor.fetchone()[0]

        if count > 0:
            return redirect(url_for(
                "students",
                delete_error="history"
            ))

        # Delete student if no borrowing history exists
        cursor.execute("""
            DELETE FROM members
            WHERE member_id = %s
        """, (member_id,))

        conn.commit()

    except mysql.connector.Error as err:
        conn.rollback()
        return f"Database error: {err}"

    finally:
        cursor.close()
        conn.close()

    return redirect(url_for("students"))

@app.route("/notifications")
def notifications():

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Find overdue books and their fines
        cursor.execute("""
            SELECT
                ib.issue_id,
                b.title,
                m.name,
                ib.due_date,
                COALESCE(f.fine_amount, 100),
                COALESCE(f.payment_status, 'Unpaid')
            FROM issued_books ib
            JOIN books b ON ib.book_id = b.book_id
            JOIN members m ON ib.member_id = m.member_id
            LEFT JOIN fines f ON ib.issue_id = f.issue_id
            WHERE ib.due_date < CURDATE()
            AND ib.return_date IS NULL
        """)

        overdue_books = cursor.fetchall()

        # Books due within the next 3 days
        cursor.execute("""
            SELECT ib.issue_id, b.title, m.name, ib.due_date
            FROM issued_books ib
            JOIN books b ON ib.book_id = b.book_id
            JOIN members m ON ib.member_id = m.member_id
            WHERE ib.due_date BETWEEN CURDATE()
            AND DATE_ADD(CURDATE(), INTERVAL 3 DAY)
            AND ib.return_date IS NULL
        """)

        upcoming_books = cursor.fetchall()

        # Returned books
        cursor.execute("""
            SELECT ib.issue_id, b.title, m.name, ib.return_date
            FROM issued_books ib
            JOIN books b ON ib.book_id = b.book_id
            JOIN members m ON ib.member_id = m.member_id
            WHERE ib.return_date IS NOT NULL
            ORDER BY ib.return_date DESC
        """)

        returned_books = cursor.fetchall()

    except mysql.connector.Error as err:
        return f"Database error: {err}"

    finally:
        cursor.close()
        conn.close()

    return render_template(
        "notifications.html",
        overdue_books=overdue_books,
        upcoming_books=upcoming_books,
        returned_books=returned_books
    )
@app.route("/mark-fine-paid/<int:issue_id>", methods=["POST"])
def mark_fine_paid(issue_id):

    if not session.get("librarian_logged_in"):
        return redirect(url_for("librarian_login"))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Get the student ID
        cursor.execute("""
            SELECT member_id
            FROM issued_books
            WHERE issue_id = %s
        """, (issue_id,))

        result = cursor.fetchone()

        if not result:
            flash("Book issue not found!", "error")
            return redirect(url_for("notifications"))

        member_id = result[0]

        # Check whether fine already exists
        cursor.execute("""
            SELECT fine_id
            FROM fines
            WHERE issue_id = %s
        """, (issue_id,))

        fine = cursor.fetchone()

        if fine:
            cursor.execute("""
                UPDATE fines
                SET payment_status = 'Paid'
                WHERE issue_id = %s
            """, (issue_id,))

        else:
            cursor.execute("""
                INSERT INTO fines
                (issue_id, member_id, fine_amount,
                 payment_status, fine_date)
                VALUES (%s, %s, 100, 'Paid', CURDATE())
            """, (issue_id, member_id))

        conn.commit()

        flash("Fine payment marked as Paid!", "success")

    except mysql.connector.Error as err:
        conn.rollback()
        flash(f"Database error: {err}", "error")

    finally:
        cursor.close()
        conn.close()

    return redirect(url_for("notifications"))

@app.route("/student-login", methods=["GET", "POST"])
def student_login():
    message = None

    if request.method == "POST":
        student_id = request.form.get("student_id")
        email = request.form.get("email")

        conn = get_db_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT member_id, name
                FROM members
                WHERE member_id = %s AND email = %s
            """, (student_id, email))

            student = cursor.fetchone()

            if student:
                session["student_id"] = student[0]
                session["student_name"] = student[1]

                cursor.execute("""
                    INSERT INTO student_sessions (member_id, last_seen)
                    VALUES (%s, NOW())
                    ON DUPLICATE KEY UPDATE last_seen = NOW()
                """, (student[0],))

                conn.commit()

                return redirect(url_for("student_dashboard"))

            else:
                message = "Invalid Student ID or email address."

        except mysql.connector.Error as err:
            message = f"Database error: {err}"

        finally:
            cursor.close()
            conn.close()

    return render_template(
        "student_login.html",
        message=message
    )

@app.route("/student-books")
def student_books():

    # Check student login
    if not session.get("student_id"):
        return redirect(url_for("student_login"))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT book_id, title, author, category, quantity
            FROM books
            ORDER BY title ASC
        """)

        books = cursor.fetchall()

    finally:
        cursor.close()
        conn.close()

    return render_template(
        "student_books.html",
        books=books,
        student_name=session.get("student_name")
    )



@app.route("/student-dashboard")
def student_dashboard():

    if "student_id" not in session:
        return redirect(url_for("student_login"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        student_id = session["student_id"]

        # Automatically create fines for new overdue books
        cursor.execute("""
            INSERT INTO fines
            (issue_id, member_id, fine_amount,
             payment_status, fine_date)

            SELECT
                ib.issue_id,
                ib.member_id,
                100,
                'Unpaid',
                CURDATE()

            FROM issued_books ib

            WHERE ib.member_id = %s
              AND ib.due_date < CURDATE()
              AND ib.return_date IS NULL

              AND NOT EXISTS (
                  SELECT 1
                  FROM fines f
                  WHERE f.issue_id = ib.issue_id
              )
        """, (student_id,))

        conn.commit()

        # Get unpaid fine details
        cursor.execute("""
            SELECT
                b.title,
                ib.due_date,
                f.fine_amount,
                f.payment_status
            FROM fines f
            JOIN issued_books ib
                ON f.issue_id = ib.issue_id
            JOIN books b
                ON ib.book_id = b.book_id
            WHERE f.member_id = %s
              AND f.payment_status = 'Unpaid'
        """, (student_id,))

        fine_details = cursor.fetchall()

        # Calculate unpaid fine amount
        cursor.execute("""
            SELECT COALESCE(SUM(fine_amount), 0) AS total_fine
            FROM fines
            WHERE member_id = %s
              AND payment_status = 'Unpaid'
        """, (student_id,))

        total_fine = cursor.fetchone()["total_fine"]

    finally:
        cursor.close()
        conn.close()

    return render_template(
        "student_dashboard.html",
        student_name=session.get("student_name", "Student"),
        student_id=student_id,
        total_fine=total_fine,
        fine_details=fine_details
    )
@app.route("/student-logout")
def student_logout():

    student_id = session.get("student_id")

    if student_id:
        conn = None
        cursor = None

        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            cursor.execute("""
                DELETE FROM student_sessions
                WHERE member_id = %s
            """, (student_id,))

            conn.commit()

        except mysql.connector.Error as err:
            print("Logout error:", err)

        finally:
            if cursor:
                cursor.close()

            if conn:
                conn.close()

    session.pop("student_id", None)
    session.pop("student_name", None)

    return redirect(url_for("student_login"))



@app.route("/librarian-login", methods=["GET", "POST"])
def librarian_login():
    message = None
    username = ""

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT username, password, theme_preference
                FROM librarian
                WHERE username = %s
            """, (username,))

            librarian = cursor.fetchone()

            if librarian and check_password_hash(librarian[1], password):

                session["librarian_logged_in"] = True
                session["librarian_username"] = librarian[0]
                session["theme_preference"] = librarian[2] or "light"

                return redirect(url_for("home"))

            else:
                message = "Invalid username or password"

        finally:
            cursor.close()
            conn.close()

    return render_template(
        "librarian_login.html",
        message=message,
        username=username
    )
@app.route("/librarian-profile", methods=["GET", "POST"])
def librarian_profile():

    if not session.get("librarian_logged_in"):
        return redirect(url_for("librarian_login"))

    message = None
    message_type = None

    current_username = session.get("librarian_username")

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        if request.method == "POST":

            full_name = request.form.get("full_name", "").strip()
            new_username = request.form.get("username", "").strip()

            if not full_name or not new_username:
                message = "Please fill in all required fields."
                message_type = "error"

            else:
                cursor.execute("""
                    SELECT librarian_id
                    FROM librarian
                    WHERE username = %s
                    AND username != %s
                """, (new_username, current_username))

                existing_user = cursor.fetchone()

                if existing_user:
                    message = "Username already exists. Choose another."
                    message_type = "error"

                else:
                    cursor.execute("""
                        UPDATE librarian
                        SET full_name = %s, username = %s
                        WHERE username = %s
                    """, (full_name, new_username, current_username))

                    conn.commit()

                    session["librarian_username"] = new_username

                    current_username = new_username

                    message = "Profile updated successfully!"
                    message_type = "success"

        cursor.execute("""
            SELECT full_name, username
            FROM librarian
            WHERE username = %s
        """, (current_username,))

        librarian = cursor.fetchone()

        librarian_name = librarian[0] or "Librarian"
        librarian_username = librarian[1]

    finally:
        cursor.close()
        conn.close()

    return render_template(
        "librarian_profile.html",
        librarian_name=librarian_name,
        librarian_username=librarian_username,
        message=message,
        message_type=message_type
    )


@app.route("/account-settings", methods=["GET", "POST"])
def account_settings():

    if not session.get("librarian_logged_in"):
        return redirect(url_for("librarian_login"))

    message = None
    message_type = None
    message_section = None

    username = session.get("librarian_username")

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        if request.method == "POST":

            action = request.form.get("action")

            # Enable / Disable Notifications
            if action == "notifications":
                message_section = "notifications"

                notifications_enabled = request.form.get(
                    "notifications_enabled"
                )

                if notifications_enabled not in ("0", "1"):
                    message = "Invalid notification setting."
                    message_type = "error"

                else:
                    cursor.execute("""
                        UPDATE librarian
                        SET notifications_enabled = %s
                        WHERE username = %s
                    """, (notifications_enabled, username))

                    conn.commit()

                    message = "Notification settings updated successfully!"
                    message_type = "success"

            # Change Password
            elif action == "change_password":
                message_section = "password"

                current_password = request.form.get("current_password")
                new_password = request.form.get("new_password")
                confirm_password = request.form.get("confirm_password")

                cursor.execute(
                    "SELECT password FROM librarian WHERE username = %s",
                    (username,)
                )

                librarian = cursor.fetchone()

                if not librarian or not check_password_hash(
                    librarian[0], current_password
                ):
                    message = "Current password is incorrect."
                    message_type = "error"

                elif new_password != confirm_password:
                    message = "New passwords do not match."
                    message_type = "error"

                elif len(new_password) < 8:
                    message = "Password must contain at least 8 characters."
                    message_type = "error"

                else:
                    hashed_password = generate_password_hash(new_password)

                    cursor.execute("""
                        UPDATE librarian
                        SET password = %s
                        WHERE username = %s
                    """, (hashed_password, username))

                    conn.commit()

                    message = "Password updated successfully!"
                    message_type = "success"

            # Dark Mode / Light Mode
            elif action == "theme":

                message_section = "theme"

                theme_preference = request.form.get("theme_preference")

                if theme_preference not in ("light", "dark"):
                    message = "Invalid theme selection."
                    message_type = "error"

                else:
                    cursor.execute("""
                        UPDATE librarian
                        SET theme_preference = %s
                        WHERE username = %s
                    """, (theme_preference, username))

                    conn.commit()
                    session["theme_preference"] = theme_preference

                    message = "Theme updated successfully!"
                    message_type = "success"

        # Get current notification and theme settings
        cursor.execute("""
            SELECT notifications_enabled, theme_preference
            FROM librarian
            WHERE username = %s
        """, (username,))

        result = cursor.fetchone()

        notifications_enabled = result[0] if result else 1
        theme_preference = result[1] if result and result[1] else "light"

    finally:
        cursor.close()
        conn.close()

    return render_template(
        "account_settings.html",
        librarian_name=username or "Librarian",
        notifications_enabled=notifications_enabled,
        theme_preference=theme_preference,
        message=message,
        message_type=message_type,
        message_section=message_section
    )



@app.route("/librarian-logout")
def librarian_logout():

    session.pop("librarian_logged_in", None)
    session.pop("librarian_username", None)

    return redirect(url_for("librarian_login"))

@app.route("/books")
def book_catalog():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            b.book_id,
            b.title,
            b.author,
            b.category,
            b.quantity AS quantity,
            COALESCE(i.issued_copies, 0) AS issued_copies,
            GREATEST(
                b.quantity - COALESCE(i.issued_copies, 0),
                0
            ) AS available_copies
        FROM books b
        LEFT JOIN (
            SELECT book_id, COUNT(*) AS issued_copies
            FROM issued_books
            WHERE return_date IS NULL
            GROUP BY book_id
        ) i ON b.book_id = i.book_id
        ORDER BY b.book_id
    """)

    books = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("books.html", books=books)

@app.route("/my-borrowed-books")
def my_borrowed_books():

    if "student_id" not in session:
        return redirect(url_for("student_login"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            issued_books.issue_id,
            books.title,
            books.author,
            issued_books.issue_date,
            issued_books.due_date,
            CASE
                WHEN issued_books.due_date < CURDATE()
                THEN 'Overdue'
                ELSE 'Issued'
            END AS status
        FROM issued_books
        JOIN books
            ON issued_books.book_id = books.book_id
        WHERE issued_books.member_id = %s
          AND issued_books.return_date IS NULL
    """, (session["student_id"],))

    borrowed_books = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "my_borrowed_books.html",
        borrowed_books=borrowed_books,
        student_name=session.get("student_name", "Student")
    )


@app.route("/student-due-dates")
def student_due_dates():

    if "student_id" not in session:
        return redirect(url_for("student_login"))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            books.title,
            books.author,
            issued_books.issue_date,
            issued_books.due_date,
            DATEDIFF(issued_books.due_date, CURDATE()) AS days_remaining,
            CASE
                WHEN issued_books.due_date < CURDATE()
                    THEN 'Overdue'
                WHEN issued_books.due_date = CURDATE()
                    THEN 'Due Today'
                WHEN issued_books.due_date <= DATE_ADD(CURDATE(), INTERVAL 3 DAY)
                    THEN 'Due Soon'
                ELSE 'Issued'
            END AS status
        FROM issued_books
        JOIN books
            ON issued_books.book_id = books.book_id
        WHERE issued_books.member_id = %s
          AND issued_books.return_date IS NULL
        ORDER BY issued_books.due_date ASC
    """, (session["student_id"],))

    due_books = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "student_due_dates.html",
        due_books=due_books,
        student_name=session.get("student_name", "Student")
    )


@app.route("/add-book", methods=["POST"])
def add_book():
    title = request.form.get("title", "").strip()
    author = request.form.get("author", "").strip()
    category = request.form.get("category", "").strip()
    quantity = request.form.get("quantity", "").strip()

    if not title or not author or not category or not quantity:
       return "Please fill in all book details.", 400

    try:
        quantity = int(quantity)

        if quantity < 1:
            return "Total copies must be at least 1.", 400
    except ValueError:
        return "Please enter a valid quantity.", 400

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
    """
    INSERT INTO books (title, author, category, quantity)
    VALUES (%s, %s, %s, %s)
    """,
    (title, author, category, quantity)
)

    conn.commit()
    cursor.close()
    conn.close()

    flash("Book added successfully!", "success")
    return redirect(url_for("book_catalog"))
    

@app.route("/delete-book/<int:book_id>", methods=["POST"])
def delete_book(book_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
    "SELECT COUNT(*) FROM issued_books WHERE book_id = %s AND return_date IS NULL",
    (book_id,)
)
    active_issues = cursor.fetchone()[0]

    if active_issues > 0:
       cursor.close()
       conn.close()
       return "This book cannot be deleted because it has an active issue.", 400

    cursor.execute(
    "DELETE FROM books WHERE book_id = %s",
    (book_id,)
)

    conn.commit()
    cursor.close()
    conn.close()

    flash("Book deleted successfully!", "success")
    return redirect(url_for("book_catalog"))

@app.route("/edit-book/<int:book_id>", methods=["GET", "POST"])
def edit_book(book_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        author = request.form.get("author", "").strip()
        category = request.form.get("category", "").strip()
        quantity = request.form.get("quantity", "").strip()

        if not title or not author or not category or not quantity:
            cursor.close()
            conn.close()
            return "Please fill in all fields."

        try:
            quantity = int(quantity)

            if quantity < 1:
                cursor.close()
                conn.close()
                return "Total copies must be at least 1."

            cursor.execute("""
                SELECT COUNT(*) AS issued_copies
                FROM issued_books
                WHERE book_id = %s AND return_date IS NULL
            """, (book_id,))

            issued_copies = cursor.fetchone()["issued_copies"]

            if quantity < issued_copies:
                cursor.close()
                conn.close()
                return f"Total copies cannot be less than issued copies ({issued_copies})."

            cursor.execute("""
                UPDATE books
                SET title = %s, author = %s, category = %s, quantity = %s
                WHERE book_id = %s
            """, (title, author, category, quantity, book_id))

            conn.commit()

        except ValueError:
            cursor.close()
            conn.close()
            return "Please enter a valid number for total copies."

        cursor.close()
        conn.close()

        return redirect(url_for("book_catalog"))

    cursor.execute("SELECT * FROM books WHERE book_id = %s", (book_id,))
    book = cursor.fetchone()

    cursor.close()
    conn.close()

    if book is None:
        return "Book not found.", 404

    return render_template("edit_book.html", book=book)


@app.route("/test-db") 
def test_db():
    try:
        conn = get_db_connection()
        conn.close() 
        return "SmartLib database connected successfully!"
    except mysql.connector.Error as err:
        return f"Database connection failed: {err}"

@app.route("/issue-return", methods=["GET", "POST"] )
def issue_return():
    if request.method == "POST":
        print("FROM SUBMITTED")
        student_id = request.form.get("student_id")
        book_id = request.form.get("book_id")
        due_date = request.form.get("due_date")
        action = request.form.get("action")
        print("FROM SUBMITTED")
        print("Action received:", action)
        if action == "return":
            issue_id = request.form.get("issue_id")

            conn = get_db_connection()
            cursor = conn.cursor()

            try:
                cursor.execute("""
                    SELECT issue_id
                    FROM issued_books
                    WHERE issue_id = %s AND return_date IS NULL
                """, (issue_id,))

                issue = cursor.fetchone()

                if not issue:
                    return "Invalid Issue ID or book already returned."

                cursor.execute("""
                    UPDATE issued_books
                    SET return_date = CURDATE()
                    WHERE issue_id = %s
                """, (issue_id,))

                conn.commit()

                
                flash("Book returned successfully!", "success")
                return redirect(url_for("issue_return"))


            except mysql.connector.Error as err:
                conn.rollback()
                return f"Database error: {err}"

            finally:
                cursor.close()
                conn.close()
        print("Student ID:", student_id)
        print("Book ID:", book_id)
        print("Due Date:", due_date)
        print("Action:", action)
        

        if action == "issue":
            conn = get_db_connection()
            cursor = conn.cursor()

            try:
                # Check whether the student exists
                cursor.execute(
                    "SELECT member_id FROM members WHERE member_id = %s",
                    (student_id,)
                )
                student = cursor.fetchone()

                if not student:
                    return "Student ID not found. Please register the student first."

                # Check whether the book exists
                cursor.execute(
                    "SELECT quantity FROM books WHERE book_id = %s",
                    (book_id,)
                )
                book = cursor.fetchone()

                if not book:
                    return "Book ID not found."

                # Count copies currently issued for this book
                cursor.execute("""
                    SELECT COUNT(*)
                    FROM issued_books
                    WHERE book_id = %s AND return_date IS NULL
                """, (book_id,))

                issued = cursor.fetchone()[0]
                available = book[0] - issued

                if available <= 0:
                    return "This book is currently unavailable."

                # Record the book issue
                cursor.execute("""
                    INSERT INTO issued_books
                    (book_id, member_id, issue_date, due_date)
                    VALUES (%s, %s, CURDATE(), %s)
                """, (book_id, student_id, due_date))

                conn.commit()

                flash("Book issued successfully!","success")
                return redirect(url_for("issue_return"))
                

            except mysql.connector.Error as err:
                conn.rollback()
                return f"Database error: {err}"

            finally:
                cursor.close()
                conn.close()
                

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
    SELECT issue_id,
           member_id AS student_id,
           book_id,
           issue_date,
           due_date,
           return_date,
           CASE
               WHEN return_date IS NOT NULL THEN 'Returned'
               WHEN due_date < CURDATE() THEN 'Overdue'
               ELSE 'Issued'
           END AS status
    FROM issued_books
    ORDER BY issue_id DESC
""")

        issued_books = cursor.fetchall()

    finally:
        cursor.close()
        conn.close()

    return render_template(
        "issue_return.html",
        issued_books=issued_books
    )
if __name__ == "__main__": 
    app.run(debug=True)