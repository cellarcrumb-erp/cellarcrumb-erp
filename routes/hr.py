from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from functools import wraps
from database import get_db, next_ref, log_audit
from datetime import datetime, date

hr_bp = Blueprint('hr', __name__, url_prefix='/hr')

from auth import login_required, require_permission

@hr_bp.route('/')
@login_required
@require_permission('hr', 'read')
def index():
    conn = get_db()
    total_employees = conn.execute('SELECT COUNT(*) FROM employees').fetchone()[0]
    active_employees = conn.execute("SELECT COUNT(*) FROM employees WHERE status='Active'").fetchone()[0]
    on_leave = conn.execute("SELECT COUNT(*) FROM employees WHERE status='On Leave'").fetchone()[0]
    dept_count = conn.execute('SELECT COUNT(*) FROM departments').fetchone()[0]
    return render_template('hr/index.html', total_employees=total_employees,
                           active_employees=active_employees, on_leave=on_leave, dept_count=dept_count)

@hr_bp.route('/departments')
@login_required
def departments():
    conn = get_db()
    depts = conn.execute('''
        SELECT d.*, COUNT(e.id) as emp_count, m.full_name as manager_name
        FROM departments d 
        LEFT JOIN employees e ON d.id = e.department_id 
        LEFT JOIN employees m ON d.manager_id = m.id
        GROUP BY d.id
    ''').fetchall()
    return render_template('hr/departments.html', departments=depts)

@hr_bp.route('/departments/new', methods=['GET', 'POST'])
@login_required
def new_department():
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name')
        manager_id = request.form.get('manager_id') or None
        if name:
            conn.execute('INSERT INTO departments (name, manager_id, is_active) VALUES (?, ?, 1)', (name, manager_id))
            conn.commit()
            log_audit(conn, session.get('user_id'), 'HR', 'Create', 'Department', name, f'Created dept {name}')
            flash('Department created successfully.', 'success')
            return redirect(url_for('hr.departments'))
    employees = conn.execute("SELECT * FROM employees WHERE status='Active' ORDER BY full_name").fetchall()
    return render_template('hr/department_form.html', dept=None, employees=employees)

@hr_bp.route('/departments/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_department(id):
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name')
        manager_id = request.form.get('manager_id') or None
        conn.execute('UPDATE departments SET name = ?, manager_id = ? WHERE id = ?', (name, manager_id, id))
        conn.commit()
        flash('Department updated.', 'success')
        return redirect(url_for('hr.departments'))
    dept = conn.execute('SELECT * FROM departments WHERE id = ?', (id,)).fetchone()
    employees = conn.execute("SELECT * FROM employees WHERE status='Active' ORDER BY full_name").fetchall()
    return render_template('hr/department_form.html', dept=dept, employees=employees)

@hr_bp.route('/job-titles')
@login_required
def job_titles():
    conn = get_db()
    titles = conn.execute('''
        SELECT j.*, d.name as department_name, COUNT(e.id) as emp_count
        FROM job_titles j
        LEFT JOIN departments d ON j.department_id = d.id
        LEFT JOIN employees e ON j.id = e.job_title_id
        GROUP BY j.id
    ''').fetchall()
    return render_template('hr/job_titles.html', job_titles=titles)

@hr_bp.route('/job-titles/new', methods=['GET', 'POST'])
@login_required
def new_job_title():
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name')
        department_id = request.form.get('department_id') or None
        if name:
            conn.execute('INSERT INTO job_titles (name, department_id) VALUES (?, ?)', (name, department_id))
            conn.commit()
            flash('Job title created.', 'success')
            return redirect(url_for('hr.job_titles'))
    depts = conn.execute('SELECT * FROM departments WHERE is_active=1').fetchall()
    return render_template('hr/job_title_form.html', title=None, departments=depts)

@hr_bp.route('/job-titles/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_job_title(id):
    conn = get_db()
    if request.method == 'POST':
        name = request.form.get('name')
        department_id = request.form.get('department_id') or None
        conn.execute('UPDATE job_titles SET name = ?, department_id = ? WHERE id = ?', (name, department_id, id))
        conn.commit()
        flash('Job title updated.', 'success')
        return redirect(url_for('hr.job_titles'))
    t = conn.execute('SELECT * FROM job_titles WHERE id = ?', (id,)).fetchone()
    depts = conn.execute('SELECT * FROM departments WHERE is_active=1').fetchall()
    return render_template('hr/job_title_form.html', title=t, departments=depts)

@hr_bp.route('/employees')
@login_required
def employees():
    conn = get_db()
    search = request.args.get('search', '').strip()
    dept_id = request.args.get('dept_id')
    status = request.args.get('status')
    
    query = '''
        SELECT e.*, d.name as department, j.name as job_title 
        FROM employees e 
        LEFT JOIN departments d ON e.department_id = d.id 
        LEFT JOIN job_titles j ON e.job_title_id = j.id
        WHERE 1=1
    '''
    params = []
    if search:
        query += ' AND (e.full_name LIKE ? OR e.emp_no LIKE ? OR e.email LIKE ?)'
        p = f'%{search}%'
        params.extend([p, p, p])
    if dept_id:
        query += ' AND e.department_id = ?'
        params.append(dept_id)
    if status:
        query += ' AND e.status = ?'
        params.append(status)
    query += ' ORDER BY e.id DESC'
    
    employees_list = conn.execute(query, params).fetchall()
    depts = conn.execute('SELECT * FROM departments WHERE is_active=1').fetchall()
    return render_template('hr/employee_list.html', employees=employees_list, departments=depts)

@hr_bp.route('/employees/new', methods=['GET', 'POST'])
@login_required
def new_employee():
    conn = get_db()
    if request.method == 'POST':
        emp_no = request.form.get('emp_no') or f"EMP-{conn.execute('SELECT COUNT(*)+1 FROM employees').fetchone()[0]:04d}"
        full_name = request.form.get('full_name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        address = request.form.get('address')
        department_id = request.form.get('department_id') or None
        job_title_id = request.form.get('job_title_id') or None
        employment_type = request.form.get('employment_type', 'Full Time')
        date_hired = request.form.get('date_hired', date.today().isoformat())
        monthly_salary = request.form.get('monthly_salary', 0, type=float)
        emergency_contact = request.form.get('emergency_contact')
        emergency_phone = request.form.get('emergency_phone')
        national_id = request.form.get('national_id')
        kra_pin = request.form.get('kra_pin')
        notes = request.form.get('notes')
        status = request.form.get('status', 'Active')
        
        if not full_name:
            flash('Employee full name is required.', 'error')
        else:
            try:
                conn.execute('''
                    INSERT INTO employees (emp_no, full_name, email, phone, address, department_id, job_title_id, employment_type, date_hired, monthly_salary, emergency_contact, emergency_phone, national_id, kra_pin, notes, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (emp_no, full_name, email, phone, address, department_id, job_title_id, employment_type, date_hired, monthly_salary, emergency_contact, emergency_phone, national_id, kra_pin, notes, status))
                conn.commit()
                log_audit(conn, session.get('user_id'), 'HR', 'Create', 'Employee', emp_no, f'Added employee {full_name}')
                flash(f'Employee {full_name} ({emp_no}) created successfully.', 'success')
                return redirect(url_for('hr.employees'))
            except Exception as e:
                flash(f'Error creating employee: {e}', 'error')
                
    depts = conn.execute('SELECT * FROM departments WHERE is_active=1').fetchall()
    titles = conn.execute('SELECT * FROM job_titles').fetchall()
    next_no = f"EMP-{conn.execute('SELECT COUNT(*)+1 FROM employees').fetchone()[0]:04d}"
    return render_template('hr/employee_form.html', emp=None, departments=depts, job_titles=titles, next_no=next_no)

@hr_bp.route('/employees/<int:id>')
@login_required
def employee_detail(id):
    conn = get_db()
    emp = conn.execute('''
        SELECT e.*, d.name as department_name, j.name as job_title_name 
        FROM employees e 
        LEFT JOIN departments d ON e.department_id = d.id 
        LEFT JOIN job_titles j ON e.job_title_id = j.id
        WHERE e.id = ?
    ''', (id,)).fetchone()
    leaves = conn.execute('SELECT * FROM leave_records WHERE employee_id=? ORDER BY id DESC', (id,)).fetchall()
    return render_template('hr/employee_detail.html', emp=emp, leaves=leaves)

view_employee = employee_detail

@hr_bp.route('/employees/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_employee(id):
    conn = get_db()
    if request.method == 'POST':
        full_name = request.form.get('full_name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        address = request.form.get('address')
        department_id = request.form.get('department_id') or None
        job_title_id = request.form.get('job_title_id') or None
        employment_type = request.form.get('employment_type', 'Full Time')
        date_hired = request.form.get('date_hired')
        monthly_salary = request.form.get('monthly_salary', 0, type=float)
        emergency_contact = request.form.get('emergency_contact')
        emergency_phone = request.form.get('emergency_phone')
        national_id = request.form.get('national_id')
        kra_pin = request.form.get('kra_pin')
        notes = request.form.get('notes')
        status = request.form.get('status', 'Active')
        
        conn.execute('''
            UPDATE employees SET full_name=?, email=?, phone=?, address=?, department_id=?, job_title_id=?,
                                 employment_type=?, date_hired=?, monthly_salary=?, emergency_contact=?,
                                 emergency_phone=?, national_id=?, kra_pin=?, notes=?, status=?
            WHERE id=?
        ''', (full_name, email, phone, address, department_id, job_title_id, employment_type, date_hired, monthly_salary, emergency_contact, emergency_phone, national_id, kra_pin, notes, status, id))
        conn.commit()
        flash('Employee record updated.', 'success')
        return redirect(url_for('hr.employee_detail', id=id))
        
    emp = conn.execute('SELECT * FROM employees WHERE id=?', (id,)).fetchone()
    depts = conn.execute('SELECT * FROM departments WHERE is_active=1').fetchall()
    titles = conn.execute('SELECT * FROM job_titles').fetchall()
    return render_template('hr/employee_form.html', emp=emp, departments=depts, job_titles=titles)

@hr_bp.route('/employees/<int:id>/terminate', methods=['POST'])
@login_required
def terminate_employee(id):
    conn = get_db()
    today = date.today().isoformat()
    conn.execute("UPDATE employees SET status='Inactive', date_terminated=? WHERE id=?", (today, id))
    conn.commit()
    flash('Employee employment terminated.', 'warning')
    return redirect(url_for('hr.employee_detail', id=id))

@hr_bp.route('/leaves')
@login_required
def leaves():
    conn = get_db()
    leaves_list = conn.execute('''
        SELECT l.*, e.full_name as employee_name, e.emp_no, u.username as approved_by_name
        FROM leave_records l
        JOIN employees e ON l.employee_id = e.id
        LEFT JOIN users u ON l.approved_by = u.id
        ORDER BY l.id DESC
    ''').fetchall()
    return render_template('hr/leave_list.html', leaves=leaves_list)

@hr_bp.route('/leaves/new', methods=['GET', 'POST'])
@login_required
def new_leave():
    conn = get_db()
    if request.method == 'POST':
        employee_id = request.form.get('employee_id')
        leave_type = request.form.get('leave_type', 'Annual')
        start_date = request.form.get('start_date')
        end_date = request.form.get('end_date')
        remarks = request.form.get('remarks', '')
        
        if employee_id and start_date and end_date:
            try:
                d1 = datetime.strptime(start_date, '%Y-%m-%d')
                d2 = datetime.strptime(end_date, '%Y-%m-%d')
                days = max(1, (d2 - d1).days + 1)
                conn.execute('''
                    INSERT INTO leave_records (employee_id, leave_type, start_date, end_date, days, status, remarks)
                    VALUES (?, ?, ?, ?, ?, 'Pending', ?)
                ''', (employee_id, leave_type, start_date, end_date, days, remarks))
                conn.commit()
                flash('Leave request submitted.', 'success')
                return redirect(url_for('hr.leaves'))
            except Exception as e:
                flash(f'Error creating leave: {e}', 'error')
    employees = conn.execute("SELECT * FROM employees WHERE status='Active' ORDER BY full_name").fetchall()
    return render_template('hr/leave_form.html', leave=None, employees=employees)

@hr_bp.route('/leaves/<int:id>')
@login_required
def view_leave(id):
    return redirect(url_for('hr.leaves'))

leave_detail = view_leave

@hr_bp.route('/leaves/<int:id>/approve', methods=['POST'])
@login_required
def approve_leave(id):
    conn = get_db()
    conn.execute("UPDATE leave_records SET status='Approved', approved_by=? WHERE id=?", (session.get('user_id'), id))
    leave = conn.execute("SELECT * FROM leave_records WHERE id=?", (id,)).fetchone()
    if leave:
        conn.execute("UPDATE employees SET status='On Leave' WHERE id=?", (leave['employee_id'],))
    conn.commit()
    flash('Leave request approved.', 'success')
    return redirect(url_for('hr.leaves'))

@hr_bp.route('/leaves/<int:id>/reject', methods=['POST'])
@login_required
def reject_leave(id):
    conn = get_db()
    conn.execute("UPDATE leave_records SET status='Rejected', approved_by=? WHERE id=?", (session.get('user_id'), id))
    conn.commit()
    flash('Leave request rejected.', 'warning')
    return redirect(url_for('hr.leaves'))

@hr_bp.route('/reports/headcount')
@login_required
def report_headcount():
    conn = get_db()
    dept_summary = conn.execute('''
        SELECT d.name as dept_name, 
               COUNT(e.id) as total,
               SUM(CASE WHEN e.status='Active' THEN 1 ELSE 0 END) as active,
               SUM(CASE WHEN e.status='On Leave' THEN 1 ELSE 0 END) as on_leave,
               SUM(CASE WHEN e.status='Inactive' THEN 1 ELSE 0 END) as inactive
        FROM departments d
        LEFT JOIN employees e ON d.id = e.department_id
        GROUP BY d.id
    ''').fetchall()
    type_summary = conn.execute('''
        SELECT employment_type, COUNT(*) as count 
        FROM employees 
        GROUP BY employment_type
    ''').fetchall()
    totals = conn.execute('''
        SELECT COUNT(*) as total,
               SUM(CASE WHEN status='Active' THEN 1 ELSE 0 END) as active,
               SUM(CASE WHEN status='On Leave' THEN 1 ELSE 0 END) as on_leave,
               SUM(CASE WHEN status='Inactive' THEN 1 ELSE 0 END) as inactive
        FROM employees
    ''').fetchone()
    return render_template('hr/report_headcount.html', dept_summary=dept_summary, type_summary=type_summary, totals=totals)

@hr_bp.route('/reports/leave-summary')
@login_required
def report_leave():
    conn = get_db()
    from_date = request.args.get('from_date', date.today().replace(month=1, day=1).isoformat())
    to_date = request.args.get('to_date', date.today().isoformat())
    leave_summary = conn.execute('''
        SELECT e.full_name as employee_name, e.emp_no, d.name as dept_name,
               SUM(CASE WHEN l.leave_type='Annual' AND l.status='Approved' THEN l.days ELSE 0 END) as annual_days,
               SUM(CASE WHEN l.leave_type='Sick' AND l.status='Approved' THEN l.days ELSE 0 END) as sick_days,
               SUM(CASE WHEN l.leave_type NOT IN ('Annual','Sick') AND l.status='Approved' THEN l.days ELSE 0 END) as other_days,
               SUM(CASE WHEN l.status='Approved' THEN l.days ELSE 0 END) as total_days
        FROM employees e
        LEFT JOIN departments d ON e.department_id = d.id
        LEFT JOIN leave_records l ON e.id = l.employee_id AND l.start_date >= ? AND l.end_date <= ?
        GROUP BY e.id
    ''', (from_date, to_date)).fetchall()
    return render_template('hr/report_leave.html', leave_summary=leave_summary, from_date=from_date, to_date=to_date)
