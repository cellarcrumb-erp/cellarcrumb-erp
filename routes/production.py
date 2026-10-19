from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
from functools import wraps
from database import get_db, next_ref, update_stock, post_journal, log_audit

production_bp = Blueprint('production', __name__, url_prefix='/production')

from auth import login_required, require_permission

@production_bp.route('/')
@login_required
@require_permission('production', 'read')
def index():
    return render_template('production/index.html')

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
        quantity_produced = float(request.form.get('quantity_produced', 1))
        notes = request.form.get('notes', '')
        
        item_ids = request.form.getlist('item_id[]')
        quantities = request.form.getlist('quantity[]')
        uom_ids = request.form.getlist('uom_id[]')
        
        if not finished_item_id or not name:
            flash('Finished item and name are required.', 'error')
            return redirect(request.url)
            
        cursor = db.cursor()
        cursor.execute(
            'INSERT INTO bom (finished_item_id, name, quantity_produced, is_active) VALUES (?, ?, ?, 1)',
            (finished_item_id, name, quantity_produced)
        )
        bom_id = cursor.lastrowid
        
        for i in range(len(item_ids)):
            if item_ids[i] and quantities[i]:
                cursor.execute(
                    'INSERT INTO bom_lines (bom_id, item_id, quantity, uom_id) VALUES (?, ?, ?, ?)',
                    (bom_id, item_ids[i], float(quantities[i]), uom_ids[i] if uom_ids[i] else None)
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
        quantity_produced = float(request.form.get('quantity_produced', 1))
        
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
                    (id, item_ids[i], float(quantities[i]), uom_ids[i] if uom_ids[i] else None)
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
        planned_qty = float(request.form.get('planned_qty', 0))
        batch_no = request.form.get('batch_no', '')
        remarks = request.form.get('remarks', '')
        doc_date = request.form.get('doc_date')
        
        doc_ref = next_ref(db, 'Production', 'Order')
        cursor = db.cursor()
        cursor.execute('''
            INSERT INTO production_orders (doc_ref, doc_date, bom_id, warehouse_id, planned_qty, status, batch_no)
            VALUES (?, ?, ?, ?, ?, 'Draft', ?)
        ''', (doc_ref, doc_date, bom_id, warehouse_id, planned_qty, batch_no))
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
    
    factor = order['planned_qty'] / order['bom_base_qty']
    materials = []
    total_est_cost = 0
    for line in bom_lines:
        req_qty = line['base_qty'] * factor
        est_cost = req_qty * line['unit_cost']
        total_est_cost += est_cost
        materials.append({
            'item_id': line['item_id'],
            'name': line['name'],
            'unit_cost': line['unit_cost'],
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
    
    if order['status'] != 'Draft':
        flash('Only draft orders can be started.', 'error')
        return redirect(url_for('production.order_detail', id=id))
        
    bom_lines = db.execute('SELECT item_id, quantity FROM bom_lines WHERE bom_id = ?', (order['bom_id'],)).fetchall()
    factor = order['planned_qty'] / order['bom_base_qty']
    
    total_cost = 0
    for line in bom_lines:
        req_qty = line['quantity'] * factor
        # get item cost
        item = db.execute('SELECT cost FROM items WHERE id = ?', (line['item_id'],)).fetchone()
        cost = item['cost'] if item['cost'] else 0
        total_cost += req_qty * cost
        
        # Issue materials
        update_stock(db, line['item_id'], order['warehouse_id'], 0, req_qty, 'ProductionIssue', order['id'], order['doc_ref'], cost, session['user_id'])
        
    # Journal for material issue: Dr WIP/Production Cost, Cr RM Inventory
    # Assumed WIP account=1330, RM=1310
    jl = [
        {'account_id': 1330, 'debit': total_cost, 'credit': 0, 'partner_id': None, 'description': f"Materials for {order['doc_ref']}"},
        {'account_id': 1310, 'debit': 0, 'credit': total_cost, 'partner_id': None, 'description': f"Materials for {order['doc_ref']}"}
    ]
    post_journal(db, order['doc_ref'], order['doc_date'], 'Production', 'ProductionIssue', order['id'], f"Start Production {order['doc_ref']}", jl, session['user_id'])
    
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
    
    if order['status'] != 'In Progress':
        flash('Only In Progress orders can be completed.', 'error')
        return redirect(url_for('production.order_detail', id=id))
        
    produced_qty = float(request.form.get('produced_qty', 0))
    wastage_qty = float(request.form.get('wastage_qty', 0))
    
    if produced_qty <= 0:
        flash('Produced quantity must be positive.', 'error')
        return redirect(url_for('production.order_detail', id=id))
        
    unit_cost = order['total_cost'] / produced_qty if produced_qty > 0 else 0
    
    # Receive finished goods
    update_stock(db, order['finished_item_id'], order['warehouse_id'], produced_qty, 0, 'ProductionReceipt', order['id'], order['doc_ref'], unit_cost, session['user_id'])
    
    # Journal for FG receipt: Dr FG Inventory (1320), Cr WIP (1330)
    jl = [
        {'account_id': 1320, 'debit': order['total_cost'], 'credit': 0, 'partner_id': None, 'description': f"FG Receipt for {order['doc_ref']}"},
        {'account_id': 1330, 'debit': 0, 'credit': order['total_cost'], 'partner_id': None, 'description': f"FG Receipt for {order['doc_ref']}"}
    ]
    post_journal(db, order['doc_ref'], order['doc_date'], 'Production', 'ProductionReceipt', order['id'], f"Complete Production {order['doc_ref']}", jl, session['user_id'])
    
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
    return render_template('production/report_production_summary.html')

@production_bp.route('/reports/material-consumption')
@login_required
def report_material_consumption():
    return render_template('production/report_material_consumption.html')

@production_bp.route('/reports/batch-cost')
@login_required
def report_batch_cost():
    return render_template('production/report_batch_cost.html')

# Wastage simple placeholders
@production_bp.route('/wastage')
@login_required
def wastage_list():
    return render_template('production/wastage_list.html')

@production_bp.route('/wastage/new')
@login_required
def wastage_new():
    return render_template('production/wastage_form.html')
