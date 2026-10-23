from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
from functools import wraps
from database import get_db, next_ref, update_stock, post_journal, log_audit
from datetime import date

production_bp = Blueprint('production', __name__, url_prefix='/production')

from auth import login_required, require_permission


def _coa_id(db, code, fallback_code='5240'):
    row = db.execute('SELECT id FROM coa WHERE code = ?', (str(code),)).fetchone()
    if row:
        return row['id']
    row2 = db.execute('SELECT id FROM coa WHERE code = ?', (str(fallback_code),)).fetchone()
    if row2:
        return row2['id']
    row3 = db.execute('SELECT id FROM coa WHERE is_posting = 1 LIMIT 1').fetchone()
    return row3['id'] if row3 else 1


@production_bp.route('/')
@login_required
@require_permission('production', 'read')
def index():
    db = get_db()
    total_boms = db.execute('SELECT COUNT(*) as c FROM bom WHERE is_active = 1').fetchone()['c']
    active_orders = db.execute("SELECT COUNT(*) as c FROM production_orders WHERE status IN ('Draft', 'In Progress')").fetchone()['c']
    completed_orders = db.execute("SELECT COUNT(*) as c FROM production_orders WHERE status = 'Completed'").fetchone()['c']
    total_wastage = db.execute('SELECT COALESCE(SUM(wastage_qty), 0) as w FROM production_wastage').fetchone()['w']
    return render_template('production/index.html',
                           total_boms=total_boms,
                           active_orders=active_orders,
                           completed_orders=completed_orders,
                           total_wastage=total_wastage)

# --- Bill of Materials ---
@production_bp.route('/bom')
@login_required
@require_permission('production', 'read')
def bom_list():
    db = get_db()
    search = request.args.get('search', '')
    query = '''
        SELECT b.*, i.name as finished_item_name
        FROM bom b
        JOIN items i ON b.finished_item_id = i.id
        WHERE b.name LIKE ? OR i.name LIKE ?
    '''
    boms = db.execute(query, ('%'+search+'%', '%'+search+'%')).fetchall()
    return render_template('production/bom_list.html', boms=boms, search=search)

@production_bp.route('/bom/new', methods=['GET', 'POST'])
@login_required
def bom_new():
    db = get_db()
    if request.method == 'POST':
        finished_item_id = request.form.get('finished_item_id')
        name = request.form.get('name')
        quantity_produced = float(request.form.get('quantity_produced', 1) or 1)
        notes = request.form.get('notes', '')
        
        item_ids = request.form.getlist('item_id[]')
        quantities = request.form.getlist('quantity[]')
        uom_ids = request.form.getlist('uom_id[]')
        
        if not finished_item_id or not name:
            flash('Finished item and name are required.', 'error')
            return redirect(request.url)
            
        cursor = db.cursor()
        cursor.execute(
            'INSERT INTO bom (finished_item_id, name, quantity_produced, is_active, notes) VALUES (?, ?, ?, 1, ?)',
            (finished_item_id, name, quantity_produced, notes)
        )
        bom_id = cursor.lastrowid
        
        for i in range(len(item_ids)):
            if item_ids[i] and quantities[i]:
                cursor.execute(
                    'INSERT INTO bom_lines (bom_id, item_id, quantity, uom_id) VALUES (?, ?, ?, ?)',
                    (bom_id, item_ids[i], float(quantities[i]), uom_ids[i] if i < len(uom_ids) and uom_ids[i] else None)
                )
        db.commit()
        log_audit(db, session['user_id'], 'Production', 'CREATE', 'BOM', bom_id, f'Created BOM {name}')
        flash('BOM created successfully.', 'success')
        return redirect(url_for('production.bom_list'))
        
    items = db.execute('SELECT id, name FROM items WHERE is_active = 1').fetchall()
    uoms = db.execute('SELECT id, name FROM uom').fetchall()
    return render_template('production/bom_form.html', items=items, uoms=uoms, bom=None)

@production_bp.route('/bom/<int:id>')
@login_required
def bom_detail(id):
    db = get_db()
    bom = db.execute('''
        SELECT b.*, i.name as finished_item_name
        FROM bom b JOIN items i ON b.finished_item_id = i.id WHERE b.id = ?
    ''', (id,)).fetchone()
    if not bom:
        flash('BOM not found.', 'error')
        return redirect(url_for('production.bom_list'))
        
    lines = db.execute('''
        SELECT bl.*, i.name as item_name, u.name as uom_name
        FROM bom_lines bl
        JOIN items i ON bl.item_id = i.id
        LEFT JOIN uom u ON bl.uom_id = u.id
        WHERE bl.bom_id = ?
    ''', (id,)).fetchall()
    return render_template('production/bom_detail.html', bom=bom, lines=lines)

@production_bp.route('/bom/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def bom_edit(id):
    db = get_db()
    bom = db.execute('SELECT * FROM bom WHERE id = ?', (id,)).fetchone()
    if not bom:
        return redirect(url_for('production.bom_list'))
        
    if request.method == 'POST':
        name = request.form.get('name')
        quantity_produced = float(request.form.get('quantity_produced', 1) or 1)
        
        db.execute('UPDATE bom SET name = ?, quantity_produced = ? WHERE id = ?',
                   (name, quantity_produced, id))
                   
        db.execute('DELETE FROM bom_lines WHERE bom_id = ?', (id,))
        item_ids = request.form.getlist('item_id[]')
        quantities = request.form.getlist('quantity[]')
        uom_ids = request.form.getlist('uom_id[]')
        for i in range(len(item_ids)):
            if item_ids[i] and quantities[i]:
                db.execute(
                    'INSERT INTO bom_lines (bom_id, item_id, quantity, uom_id) VALUES (?, ?, ?, ?)',
                    (id, item_ids[i], float(quantities[i]), uom_ids[i] if i < len(uom_ids) and uom_ids[i] else None)
                )
        db.commit()
        flash('BOM updated.', 'success')
        return redirect(url_for('production.bom_detail', id=id))
        
    items = db.execute('SELECT id, name FROM items WHERE is_active = 1').fetchall()
    uoms = db.execute('SELECT id, name FROM uom').fetchall()
    lines = db.execute('SELECT * FROM bom_lines WHERE bom_id = ?', (id,)).fetchall()
    return render_template('production/bom_form.html', bom=bom, lines=lines, items=items, uoms=uoms)

@production_bp.route('/bom/<int:id>/toggle-active', methods=['POST'])
@login_required
def bom_toggle_active(id):
    db = get_db()
    bom = db.execute('SELECT is_active FROM bom WHERE id = ?', (id,)).fetchone()
    if bom:
        new_status = 0 if bom['is_active'] else 1
        db.execute('UPDATE bom SET is_active = ? WHERE id = ?', (new_status, id))
        db.commit()
        flash('BOM status updated.', 'success')
    return redirect(url_for('production.bom_detail', id=id))

# --- Production Orders ---
@production_bp.route('/orders')
@login_required
def order_list():
    db = get_db()
    status = request.args.get('status', '')
    query = '''
        SELECT po.*, b.name as bom_name, i.name as product_name
        FROM production_orders po
        JOIN bom b ON po.bom_id = b.id
        JOIN items i ON b.finished_item_id = i.id
        WHERE 1=1
    '''
    params = []
    if status:
        query += ' AND po.status = ?'
        params.append(status)
    query += ' ORDER BY po.doc_date DESC, po.id DESC'
    orders = db.execute(query, params).fetchall()
    return render_template('production/order_list.html', orders=orders, status=status)

@production_bp.route('/orders/new', methods=['GET', 'POST'])
@login_required
def order_new():
    db = get_db()
    if request.method == 'POST':
        bom_id = request.form.get('bom_id')
        warehouse_id = request.form.get('warehouse_id')
        planned_qty = float(request.form.get('planned_qty', 0) or 0)
        batch_no = request.form.get('batch_no', '')
        remarks = request.form.get('remarks', '')
        doc_date = request.form.get('doc_date') or date.today().isoformat()
        
        doc_ref = next_ref('Production', 'Order', db)
        cursor = db.cursor()
        cursor.execute('''
            INSERT INTO production_orders (doc_ref, doc_date, bom_id, warehouse_id, planned_qty, status, batch_no, remarks, branch_id, posted_by)
            VALUES (?, ?, ?, ?, ?, 'Draft', ?, ?, ?, ?)
        ''', (doc_ref, doc_date, bom_id, warehouse_id, planned_qty, batch_no, remarks, session.get('active_branch_id'), session.get('user_id')))
        order_id = cursor.lastrowid
        db.commit()
        flash('Production Order created.', 'success')
        return redirect(url_for('production.order_detail', id=order_id))
        
    boms = db.execute('SELECT id, name FROM bom WHERE is_active = 1').fetchall()
    warehouses = db.execute('SELECT id, name FROM warehouses WHERE is_active = 1').fetchall()
    return render_template('production/order_form.html', boms=boms, warehouses=warehouses)

@production_bp.route('/orders/<int:id>')
@login_required
def order_detail(id):
    db = get_db()
    order = db.execute('''
        SELECT po.*, b.name as bom_name, b.quantity_produced as bom_base_qty,
               i.name as product_name, i.id as finished_item_id, w.name as warehouse_name
        FROM production_orders po
        JOIN bom b ON po.bom_id = b.id
        JOIN items i ON b.finished_item_id = i.id
        JOIN warehouses w ON po.warehouse_id = w.id
        WHERE po.id = ?
    ''', (id,)).fetchone()
    
    if not order:
        flash('Order not found', 'error')
        return redirect(url_for('production.order_list'))
        
    bom_lines = db.execute('''
        SELECT bl.item_id, i.name, i.cost as unit_cost, bl.quantity as base_qty
        FROM bom_lines bl
        JOIN items i ON bl.item_id = i.id
        WHERE bl.bom_id = ?
    ''', (order['bom_id'],)).fetchall()
    
    factor = (order['planned_qty'] or 0) / (order['bom_base_qty'] or 1)
    materials = []
    total_est_cost = 0
    for line in bom_lines:
        req_qty = line['base_qty'] * factor
        est_cost = req_qty * (line['unit_cost'] or 0)
        total_est_cost += est_cost
        materials.append({
            'item_id': line['item_id'],
            'name': line['name'],
            'unit_cost': line['unit_cost'] or 0,
            'req_qty': req_qty,
            'est_cost': est_cost
        })
        
    return render_template('production/order_detail.html', order=order, materials=materials, total_est_cost=total_est_cost)

@production_bp.route('/orders/<int:id>/start', methods=['POST'])
@login_required
def order_start(id):
    db = get_db()
    order = db.execute('''
        SELECT po.*, b.quantity_produced as bom_base_qty
        FROM production_orders po
        JOIN bom b ON po.bom_id = b.id
        WHERE po.id = ?
    ''', (id,)).fetchone()
    
    if not order or order['status'] != 'Draft':
        flash('Only draft orders can be started.', 'error')
        return redirect(url_for('production.order_detail', id=id))
        
    bom_lines = db.execute('SELECT item_id, quantity FROM bom_lines WHERE bom_id = ?', (order['bom_id'],)).fetchall()
    factor = (order['planned_qty'] or 0) / (order['bom_base_qty'] or 1)
    
    total_cost = 0
    today_str = date.today().isoformat()
    for line in bom_lines:
        req_qty = line['quantity'] * factor
        item = db.execute('SELECT cost FROM items WHERE id = ?', (line['item_id'],)).fetchone()
        cost = float(item['cost']) if item and item['cost'] else 0.0
        total_cost += req_qty * cost
        
        # Issue materials from stock
        update_stock(db, line['item_id'], order['warehouse_id'], 0, req_qty, 'ProductionIssue', order['id'], order['doc_ref'], cost, session['user_id'])
        # Record in production_material_issues
        db.execute('''
            INSERT INTO production_material_issues (production_order_id, item_id, planned_qty, issued_qty, unit_cost, issue_date)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (order['id'], line['item_id'], req_qty, req_qty, cost, today_str))
        
    # Journal for material issue: Dr Production Cost (5240), Cr RM Inventory (1310)
    wip_acc = _coa_id(db, '5240')
    rm_acc = _coa_id(db, '1310')
    if total_cost > 0:
        jl = [
            {'account_id': wip_acc, 'debit': total_cost, 'credit': 0, 'partner_id': None, 'description': f"Materials for {order['doc_ref']}"},
            {'account_id': rm_acc, 'debit': 0, 'credit': total_cost, 'partner_id': None, 'description': f"Materials for {order['doc_ref']}"}
        ]
        post_journal(db, f"{order['doc_ref']}-ISS", order['doc_date'] or today_str, 'Production', 'ProductionIssue', order['id'], f"Start Production {order['doc_ref']}", jl, session['user_id'], branch_id=session.get('active_branch_id'))
    
    db.execute("UPDATE production_orders SET status = 'In Progress', total_cost = ? WHERE id = ?", (total_cost, id))
    db.commit()
    flash('Production started and materials issued.', 'success')
    return redirect(url_for('production.order_detail', id=id))

@production_bp.route('/orders/<int:id>/complete', methods=['POST'])
@login_required
def order_complete(id):
    db = get_db()
    order = db.execute('''
        SELECT po.*, b.finished_item_id
        FROM production_orders po
        JOIN bom b ON po.bom_id = b.id
        WHERE po.id = ?
    ''', (id,)).fetchone()
    
    if not order or order['status'] != 'In Progress':
        flash('Only In Progress orders can be completed.', 'error')
        return redirect(url_for('production.order_detail', id=id))
        
    produced_qty = float(request.form.get('produced_qty', 0) or 0)
    wastage_qty = float(request.form.get('wastage_qty', 0) or 0)
    
    if produced_qty <= 0:
        flash('Produced quantity must be positive.', 'error')
        return redirect(url_for('production.order_detail', id=id))
        
    total_cost = float(order['total_cost'] or 0)
    unit_cost = total_cost / produced_qty if produced_qty > 0 else 0
    today_str = date.today().isoformat()
    
    # Receive finished goods
    update_stock(db, order['finished_item_id'], order['warehouse_id'], produced_qty, 0, 'ProductionReceipt', order['id'], order['doc_ref'], unit_cost, session['user_id'])
    
    if wastage_qty > 0:
        db.execute('''
            INSERT INTO production_wastage (production_order_id, item_id, wastage_qty, reason, doc_date)
            VALUES (?, ?, ?, ?, ?)
        ''', (order['id'], order['finished_item_id'], wastage_qty, f"Production completion wastage on {order['doc_ref']}", today_str))
    
    # Journal for FG receipt: Dr FG Inventory (1320), Cr Production Cost (5240)
    fg_acc = _coa_id(db, '1320')
    wip_acc = _coa_id(db, '5240')
    if total_cost > 0:
        jl = [
            {'account_id': fg_acc, 'debit': total_cost, 'credit': 0, 'partner_id': None, 'description': f"FG Receipt for {order['doc_ref']}"},
            {'account_id': wip_acc, 'debit': 0, 'credit': total_cost, 'partner_id': None, 'description': f"FG Receipt for {order['doc_ref']}"}
        ]
        post_journal(db, f"{order['doc_ref']}-REC", order['doc_date'] or today_str, 'Production', 'ProductionReceipt', order['id'], f"Complete Production {order['doc_ref']}", jl, session['user_id'], branch_id=session.get('active_branch_id'))
    
    db.execute("UPDATE production_orders SET status = 'Completed', produced_qty = ?, wastage_qty = ? WHERE id = ?",
               (produced_qty, wastage_qty, id))
    db.commit()
    flash('Production completed.', 'success')
    return redirect(url_for('production.order_detail', id=id))

@production_bp.route('/orders/<int:id>/cancel', methods=['POST'])
@login_required
def order_cancel(id):
    db = get_db()
    db.execute("UPDATE production_orders SET status = 'Cancelled' WHERE id = ? AND status = 'Draft'", (id,))
    db.commit()
    flash('Order cancelled.', 'success')
    return redirect(url_for('production.order_detail', id=id))

# --- Reports ---
@production_bp.route('/reports/production-summary')
@login_required
def report_production_summary():
    db = get_db()
    orders = db.execute('''
        SELECT po.*, b.name as bom_name, i.name as product_name, i.code as product_code, w.name as warehouse_name
        FROM production_orders po
        JOIN bom b ON po.bom_id = b.id
        JOIN items i ON b.finished_item_id = i.id
        LEFT JOIN warehouses w ON po.warehouse_id = w.id
        ORDER BY po.doc_date DESC, po.id DESC
    ''').fetchall()
    total_planned = sum(float(o['planned_qty'] or 0) for o in orders)
    total_produced = sum(float(o['produced_qty'] or 0) for o in orders)
    total_wastage = sum(float(o['wastage_qty'] or 0) for o in orders)
    total_cost = sum(float(o['total_cost'] or 0) for o in orders)
    return render_template('production/report_production_summary.html',
                           orders=orders,
                           total_planned=total_planned,
                           total_produced=total_produced,
                           total_wastage=total_wastage,
                           total_cost=total_cost)

@production_bp.route('/reports/material-consumption')
@login_required
def report_material_consumption():
    db = get_db()
    issues = db.execute('''
        SELECT pmi.*, po.doc_ref as order_ref, po.batch_no, i.code as item_code, i.name as item_name,
               (pmi.issued_qty * pmi.unit_cost) as total_cost
        FROM production_material_issues pmi
        LEFT JOIN production_orders po ON pmi.production_order_id = po.id
        JOIN items i ON pmi.item_id = i.id
        ORDER BY pmi.id DESC
    ''').fetchall()
    total_cost = sum(float(r['total_cost'] or 0) for r in issues)
    return render_template('production/report_material_consumption.html', issues=issues, total_cost=total_cost)

@production_bp.route('/reports/batch-cost')
@login_required
def report_batch_cost():
    db = get_db()
    orders = db.execute('''
        SELECT po.*, b.name as bom_name, i.name as product_name, i.code as product_code
        FROM production_orders po
        JOIN bom b ON po.bom_id = b.id
        JOIN items i ON b.finished_item_id = i.id
        WHERE po.status IN ('In Progress', 'Completed')
        ORDER BY po.doc_date DESC, po.id DESC
    ''').fetchall()
    batches = []
    for o in orders:
        prod_qty = float(o['produced_qty'] or o['planned_qty'] or 1)
        tot_cost = float(o['total_cost'] or 0)
        unit_cost = tot_cost / prod_qty if prod_qty > 0 else 0
        batches.append({
            'id': o['id'],
            'doc_ref': o['doc_ref'],
            'doc_date': o['doc_date'],
            'batch_no': o['batch_no'] or '-',
            'product_name': o['product_name'],
            'bom_name': o['bom_name'],
            'planned_qty': float(o['planned_qty'] or 0),
            'produced_qty': float(o['produced_qty'] or 0),
            'wastage_qty': float(o['wastage_qty'] or 0),
            'total_cost': tot_cost,
            'unit_cost': unit_cost,
            'status': o['status']
        })
    return render_template('production/report_batch_cost.html', batches=batches)

# --- Production Wastage ---
@production_bp.route('/wastage')
@login_required
def wastage_list():
    db = get_db()
    records = db.execute('''
        SELECT pw.*, po.doc_ref as order_ref, i.code as item_code, i.name as item_name, i.cost as unit_cost,
               (pw.wastage_qty * COALESCE(i.cost, 0)) as wastage_value
        FROM production_wastage pw
        LEFT JOIN production_orders po ON pw.production_order_id = po.id
        JOIN items i ON pw.item_id = i.id
        ORDER BY pw.id DESC
    ''').fetchall()
    total_value = sum(float(r['wastage_value'] or 0) for r in records)
    return render_template('production/wastage_list.html', records=records, total_value=total_value)

@production_bp.route('/wastage/new', methods=['GET', 'POST'])
@login_required
def wastage_new():
    db = get_db()
    if request.method == 'POST':
        production_order_id = request.form.get('production_order_id') or None
        item_id = request.form.get('item_id')
        warehouse_id = request.form.get('warehouse_id')
        wastage_qty = float(request.form.get('wastage_qty', 0) or 0)
        reason = request.form.get('reason', '').strip() or 'Production spoilage / wastage'
        doc_date = request.form.get('doc_date') or date.today().isoformat()
        
        if not item_id or wastage_qty <= 0:
            flash('Item and positive wastage quantity are required.', 'error')
            return redirect(request.url)
            
        item = db.execute('SELECT cost, name FROM items WHERE id = ?', (item_id,)).fetchone()
        cost = float(item['cost'] or 0) if item else 0.0
        total_loss = wastage_qty * cost
        
        cur = db.execute('''
            INSERT INTO production_wastage (production_order_id, item_id, wastage_qty, reason, doc_date)
            VALUES (?, ?, ?, ?, ?)
        ''', (production_order_id, item_id, wastage_qty, reason, doc_date))
        wastage_id = cur.lastrowid
        
        doc_ref = next_ref('Production', 'Wastage', db)
        if warehouse_id:
            update_stock(db, int(item_id), int(warehouse_id), 0, wastage_qty, 'Wastage', wastage_id, doc_ref, cost, session.get('user_id'))
            
        if total_loss > 0:
            loss_acc = _coa_id(db, '5260')
            inv_acc = _coa_id(db, '1310')
            jl = [
                {'account_id': loss_acc, 'debit': total_loss, 'credit': 0, 'partner_id': None, 'description': f"Wastage: {reason}"},
                {'account_id': inv_acc, 'debit': 0, 'credit': total_loss, 'partner_id': None, 'description': f"Inventory write-off: {reason}"}
            ]
            post_journal(db, doc_ref, doc_date, 'Production', 'Wastage', wastage_id, f"Production Wastage - {reason}", jl, session.get('user_id'), branch_id=session.get('active_branch_id'))
            
        db.commit()
        log_audit(db, session.get('user_id'), 'Production', 'Create', 'Wastage', wastage_id, f"Recorded wastage {wastage_qty} of item {item_id}")
        flash('Production wastage recorded successfully.', 'success')
        return redirect(url_for('production.wastage_list'))
        
    orders = db.execute('SELECT id, doc_ref, batch_no FROM production_orders ORDER BY id DESC').fetchall()
    items = db.execute('SELECT id, code, name, cost FROM items WHERE is_active = 1 ORDER BY name').fetchall()
    warehouses = db.execute('SELECT id, name FROM warehouses WHERE is_active = 1 ORDER BY name').fetchall()
    return render_template('production/wastage_form.html', orders=orders, items=items, warehouses=warehouses, today=date.today().isoformat())
