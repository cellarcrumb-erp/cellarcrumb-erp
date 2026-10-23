import csv
from io import StringIO
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, make_response, jsonify
from database import get_db, next_ref, update_stock, post_journal, log_audit
from auth import login_required, require_permission
from datetime import datetime, date

inventory_bp = Blueprint('inventory', __name__, url_prefix='/inventory')

@inventory_bp.route('/')
@login_required
@require_permission('inventory', 'read')
def index():
    conn = get_db()
    total_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    total_warehouses = conn.execute("SELECT COUNT(*) FROM warehouses").fetchone()[0]
    low_stock = conn.execute("SELECT COUNT(*) FROM items WHERE is_stock_item = 1 AND reorder_level > 0").fetchone()[0]
    
    total_value = 0
    stocks = conn.execute("SELECT sb.quantity, i.cost FROM stock_balances sb JOIN items i ON sb.item_id = i.id").fetchall()
    for s in stocks:
        total_value += (s['quantity'] * s['cost'])

    return render_template('inventory/index.html', 
                           total_items=total_items, 
                           total_warehouses=total_warehouses,
                           low_stock=low_stock,
                           total_value=total_value)

# --- Inventory Copy From / To APIs ---

@inventory_bp.route('/api/eligible-sources')
@login_required
def api_eligible_sources():
    target_type = request.args.get('target_type', '')
    conn = get_db()
    sources = []
    
    if target_type == 'Goods Receipt':
        pos = conn.execute("""
            SELECT p.id, p.doc_ref, p.doc_date, 'Purchase Order' as doc_type, 'purchase' as source_module,
                   bp.name as partner_name, w.name as warehouse_name, p.total,
                   (SELECT COUNT(*) FROM purchase_lines pl WHERE pl.doc_id = p.id AND (pl.quantity - COALESCE(pl.copied_qty, 0)) > 0) as open_lines_count
            FROM purchase_docs p
            LEFT JOIN partners bp ON p.supplier_id = bp.id
            LEFT JOIN warehouses w ON p.warehouse_id = w.id
            WHERE p.doc_type = 'Purchase Order' AND p.status NOT IN ('Cancelled', 'Closed')
            ORDER BY p.doc_date DESC
        """).fetchall()
        for r in pos:
            if (r['open_lines_count'] or 0) > 0:
                sources.append(dict(r))
            
    elif target_type == 'Goods Issue':
        sos = conn.execute("""
            SELECT s.id, s.doc_ref, s.doc_date, 'Sales Order' as doc_type, 'sales' as source_module,
                   bp.name as partner_name, w.name as warehouse_name, s.total,
                   (SELECT COUNT(*) FROM sales_lines sl WHERE sl.doc_id = s.id AND (sl.quantity - COALESCE(sl.copied_qty, 0)) > 0) as open_lines_count
            FROM sales_docs s
            LEFT JOIN partners bp ON s.customer_id = bp.id
            LEFT JOIN warehouses w ON s.warehouse_id = w.id
            WHERE s.doc_type = 'Sales Order' AND s.status NOT IN ('Cancelled', 'Closed')
            ORDER BY s.doc_date DESC
        """).fetchall()
        for r in sos:
            if (r['open_lines_count'] or 0) > 0:
                sources.append(dict(r))
            
    elif target_type == 'Stock Transfer':
        trans = conn.execute("""
            SELECT t.id, t.doc_ref, t.doc_date, 'Transfer' as doc_type, 'inventory' as source_module,
                   '' as partner_name, w1.name || ' -> ' || w2.name as warehouse_name, 0 as total,
                   (SELECT COUNT(*) FROM stock_transfer_lines tl WHERE tl.transfer_id = t.id AND (tl.quantity - COALESCE(tl.copied_qty, 0)) > 0) as open_lines_count
            FROM stock_transfers t
            LEFT JOIN warehouses w1 ON t.from_warehouse_id = w1.id
            LEFT JOIN warehouses w2 ON t.to_warehouse_id = w2.id
            WHERE t.status != 'Cancelled'
            ORDER BY t.doc_date DESC
        """).fetchall()
        for r in trans:
            if (r['open_lines_count'] or 0) > 0:
                sources.append(dict(r))
            
    return jsonify({'sources': sources})

@inventory_bp.route('/api/source-lines')
@login_required
def api_source_lines():
    source_module = request.args.get('source_module')
    doc_id = request.args.get('doc_id', type=int)
    conn = get_db()
    lines_data = []
    header = {}
    
    if source_module == 'purchase':
        doc = conn.execute('SELECT d.*, bp.name as partner_name, w.name as warehouse_name FROM purchase_docs d LEFT JOIN partners bp ON d.supplier_id=bp.id LEFT JOIN warehouses w ON d.warehouse_id=w.id WHERE d.id=?', (doc_id,)).fetchone()
        if doc:
            header = dict(doc)
            lines = conn.execute("""
                SELECT l.*, i.name as item_name, i.code as item_code, (l.quantity - COALESCE(l.copied_qty, 0)) as open_qty
                FROM purchase_lines l JOIN items i ON l.item_id = i.id WHERE l.doc_id = ?
            """, (doc_id,)).fetchall()
            lines_data = [dict(l) for l in lines]
            
    elif source_module == 'sales':
        doc = conn.execute('SELECT d.*, bp.name as partner_name, w.name as warehouse_name FROM sales_docs d LEFT JOIN partners bp ON d.customer_id=bp.id LEFT JOIN warehouses w ON d.warehouse_id=w.id WHERE d.id=?', (doc_id,)).fetchone()
        if doc:
            header = dict(doc)
            lines = conn.execute("""
                SELECT l.*, i.name as item_name, i.code as item_code, (l.quantity - COALESCE(l.copied_qty, 0)) as open_qty
                FROM sales_lines l JOIN items i ON l.item_id = i.id WHERE l.doc_id = ?
            """, (doc_id,)).fetchall()
            lines_data = [dict(l) for l in lines]
            
    elif source_module == 'inventory':
        doc = conn.execute('SELECT * FROM stock_transfers WHERE id=?', (doc_id,)).fetchone()
        if doc:
            header = dict(doc)
            lines = conn.execute("""
                SELECT l.*, i.name as item_name, i.code as item_code, (l.quantity - COALESCE(l.copied_qty, 0)) as open_qty
                FROM stock_transfer_lines l JOIN items i ON l.item_id = i.id WHERE l.transfer_id = ?
            """, (doc_id,)).fetchall()
            lines_data = [dict(l) for l in lines]
            
    return jsonify({'header': header, 'lines': lines_data})

# --- Items & Warehouses ---

@inventory_bp.route('/items')
@login_required
def items():
    search = request.args.get('search', '')
    conn = get_db()
    query = "SELECT i.*, (SELECT SUM(quantity) FROM stock_balances WHERE item_id = i.id) as total_stock FROM items i WHERE i.name LIKE ? OR i.code LIKE ?"
    items = conn.execute(query, (f"%{search}%", f"%{search}%")).fetchall()
    return render_template('inventory/items_list.html', items=items, search=search)

@inventory_bp.route('/items/new', methods=['GET', 'POST'])
@login_required
def new_item():
    if request.method == 'POST':
        code = request.form.get('code')
        name = request.form.get('name')
        item_type = request.form.get('item_type')
        uom_id = request.form.get('uom_id') or None
        cost = float(request.form.get('cost') or 0)
        price = float(request.form.get('price') or 0)
        reorder_level = float(request.form.get('reorder_level') or 0)
        is_stock_item = 1 if request.form.get('is_stock_item') else 0
        is_active = 1 if request.form.get('is_active') else 0

        conn = get_db()
        try:
            cur = conn.execute("""
                INSERT INTO items (code, name, item_type, uom_id, cost, price, reorder_level, is_stock_item, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (code, name, item_type, uom_id, cost, price, reorder_level, is_stock_item, is_active))
            
            new_id = cur.lastrowid
            log_audit(conn, session['user_id'], 'Inventory', 'Create Item', 'Item', new_id, f"Created item {code}")
            conn.commit()
            flash('Item created successfully', 'success')
            return redirect(url_for('inventory.items'))
        except Exception as e:
            conn.rollback()
            flash(f"Error: {str(e)}", 'danger')
            
    return render_template('inventory/item_form.html', item=None)

@inventory_bp.route('/items/<int:id>')
@login_required
def item_detail(id):
    conn = get_db()
    item = conn.execute("SELECT * FROM items WHERE id = ?", (id,)).fetchone()
    balances = conn.execute("SELECT w.name, sb.quantity FROM stock_balances sb JOIN warehouses w ON sb.warehouse_id = w.id WHERE sb.item_id = ?", (id,)).fetchall()
    ledger = conn.execute("SELECT * FROM stock_ledger WHERE item_id = ? ORDER BY id DESC LIMIT 20", (id,)).fetchall()
    return render_template('inventory/item_detail.html', item=item, balances=balances, ledger=ledger)

@inventory_bp.route('/items/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_item(id):
    conn = get_db()
    item = conn.execute("SELECT * FROM items WHERE id = ?", (id,)).fetchone()
    
    if request.method == 'POST':
        name = request.form.get('name')
        item_type = request.form.get('item_type')
        cost = float(request.form.get('cost') or 0)
        price = float(request.form.get('price') or 0)
        reorder_level = float(request.form.get('reorder_level') or 0)
        is_stock_item = 1 if request.form.get('is_stock_item') else 0
        is_active = 1 if request.form.get('is_active') else 0

        try:
            conn.execute("""
                UPDATE items SET name=?, item_type=?, cost=?, price=?, reorder_level=?, is_stock_item=?, is_active=?
                WHERE id=?
            """, (name, item_type, cost, price, reorder_level, is_stock_item, is_active, id))
            log_audit(conn, session['user_id'], 'Inventory', 'Edit Item', 'Item', id, f"Updated item {item['code']}")
            conn.commit()
            flash('Item updated successfully', 'success')
            return redirect(url_for('inventory.items'))
        except Exception as e:
            conn.rollback()
            flash(f"Error: {str(e)}", 'danger')

    return render_template('inventory/item_form.html', item=item)

@inventory_bp.route('/warehouses')
@login_required
def warehouses():
    conn = get_db()
    whs = conn.execute("""
        SELECT w.*, b.name as branch_name, b.code as branch_code 
        FROM warehouses w 
        LEFT JOIN branches b ON w.branch_id = b.id
        ORDER BY w.name ASC
    """).fetchall()
    return render_template('inventory/warehouses.html', warehouses=whs)

@inventory_bp.route('/warehouses/new', methods=['GET', 'POST'])
@login_required
def new_warehouse():
    conn = get_db()
    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        name = request.form.get('name', '').strip()
        location = request.form.get('location', '').strip()
        branch_id = request.form.get('branch_id', type=int) or session.get('active_branch_id') or None
        try:
            conn.execute("INSERT INTO warehouses (code, name, location, branch_id, is_active) VALUES (?, ?, ?, ?, 1)", (code, name, location, branch_id))
            conn.commit()
            flash('Warehouse created successfully.', 'success')
            return redirect(url_for('inventory.warehouses'))
        except Exception as e:
            conn.rollback()
            flash(f"Error creating warehouse: {e}", 'error')
    branches = conn.execute("SELECT * FROM branches WHERE is_active=1 ORDER BY is_main DESC, name ASC").fetchall()
    return render_template('inventory/warehouse_form.html', warehouse=None, branches=branches)

@inventory_bp.route('/warehouses/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_warehouse(id):
    conn = get_db()
    wh = conn.execute("SELECT * FROM warehouses WHERE id = ?", (id,)).fetchone()
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        location = request.form.get('location', '').strip()
        branch_id = request.form.get('branch_id', type=int) or None
        is_active = 1 if request.form.get('is_active') else 0
        try:
            conn.execute("UPDATE warehouses SET name=?, location=?, branch_id=?, is_active=? WHERE id=?", (name, location, branch_id, is_active, id))
            conn.commit()
            flash('Warehouse updated successfully.', 'success')
            return redirect(url_for('inventory.warehouses'))
        except Exception as e:
            conn.rollback()
            flash(f"Error updating warehouse: {e}", 'error')
    branches = conn.execute("SELECT * FROM branches WHERE is_active=1 ORDER BY is_main DESC, name ASC").fetchall()
    return render_template('inventory/warehouse_form.html', warehouse=wh, branches=branches)

@inventory_bp.route('/stock')
@login_required
def stock_balances():
    conn = get_db()
    balances = conn.execute("""SELECT i.code, i.name, w.name as warehouse, sb.quantity, i.cost, (sb.quantity * i.cost) as value 
                             FROM stock_balances sb 
                             JOIN items i ON sb.item_id = i.id 
                             JOIN warehouses w ON sb.warehouse_id = w.id""").fetchall()
    return render_template('inventory/stock_balances.html', balances=balances)

# --- Goods Receipts ---

@inventory_bp.route('/receipts')
@login_required
def receipts():
    conn = get_db()
    recs = conn.execute("""
        SELECT gr.*, w.name as warehouse_name 
        FROM goods_receipts gr 
        LEFT JOIN warehouses w ON gr.warehouse_id = w.id 
        ORDER BY gr.id DESC
    """).fetchall()
    return render_template('inventory/receipt_list.html', receipts=recs)

@inventory_bp.route('/receipts/new', methods=['GET', 'POST'])
@login_required
def new_receipt():
    conn = get_db()
    if request.method == 'POST':
        warehouse_id = request.form.get('warehouse_id')
        doc_date = request.form.get('doc_date', date.today().isoformat())
        remarks = request.form.get('remarks', '')
        source_doc_id = request.form.get('source_doc_id', type=int) or None
        source_doc_type = request.form.get('source_doc_type') or None
        source_doc_ref = request.form.get('source_doc_ref') or None
        
        items = request.form.getlist('item_id[]')
        qtys = request.form.getlist('quantity[]')
        costs = request.form.getlist('unit_cost[]')
        source_line_ids = request.form.getlist('source_line_id[]')
        
        try:
            doc_ref = next_ref('Inventory', 'GoodsReceipt', conn)
            total = sum(float(q) * float(c) for q, c in zip(qtys, costs) if q and c)
            
            cur = conn.execute("""
                INSERT INTO goods_receipts (doc_ref, doc_date, warehouse_id, status, remarks, total_value, source_doc_id, source_doc_type, source_doc_ref, posted_by)
                VALUES (?, ?, ?, 'Draft', ?, ?, ?, ?, ?, ?)
            """, (doc_ref, doc_date, warehouse_id, remarks, total, source_doc_id, source_doc_type, source_doc_ref, session.get('user_id')))
            rec_id = cur.lastrowid
            
            for i in range(len(items)):
                if items[i] and qtys[i]:
                    qty = float(qtys[i])
                    cost = float(costs[i]) if i < len(costs) and costs[i] else 0.0
                    line_total = qty * cost
                    s_line_id = int(source_line_ids[i]) if i < len(source_line_ids) and source_line_ids[i] else None
                    
                    conn.execute("""
                        INSERT INTO goods_receipt_lines (receipt_id, item_id, quantity, unit_cost, total, source_doc_type, source_doc_id, source_line_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (rec_id, items[i], qty, cost, line_total, source_doc_type, source_doc_id, s_line_id))
                    
                    if s_line_id and source_doc_type == 'Purchase Order':
                        conn.execute('UPDATE purchase_lines SET copied_qty = COALESCE(copied_qty, 0) + ? WHERE id = ?', (qty, s_line_id))
                    
            log_audit(conn, session.get('user_id'), 'Inventory', 'Create', 'Goods Receipt', rec_id, f"Created GR {doc_ref}")
            conn.commit()
            flash('Goods Receipt created successfully.', 'success')
            return redirect(url_for('inventory.receipt_detail', id=rec_id))
        except Exception as e:
            conn.rollback()
            flash(f'Error: {e}', 'danger')
            
    whs = conn.execute("SELECT * FROM warehouses WHERE is_active=1").fetchall()
    items = conn.execute("SELECT * FROM items WHERE is_stock_item=1 AND is_active=1").fetchall()
    return render_template('inventory/receipt_form.html', warehouses=whs, items=items)

@inventory_bp.route('/receipts/<int:id>')
@login_required
def receipt_detail(id):
    conn = get_db()
    rec = conn.execute("""
        SELECT gr.*, w.name as warehouse_name 
        FROM goods_receipts gr 
        LEFT JOIN warehouses w ON gr.warehouse_id = w.id 
        WHERE gr.id = ?
    """, (id,)).fetchone()
    if not rec:
        flash('Receipt not found.', 'warning')
        return redirect(url_for('inventory.receipts'))
        
    lines = conn.execute("SELECT gl.*, i.name as item_name, i.code as item_code FROM goods_receipt_lines gl JOIN items i ON gl.item_id = i.id WHERE gl.receipt_id = ?", (id,)).fetchall()
    
    base_doc = None
    if rec['source_doc_id'] and rec['source_doc_type']:
        if rec['source_doc_type'] == 'Purchase Order':
            base_doc = conn.execute('SELECT id, doc_ref, doc_date, doc_type, status, total FROM purchase_docs WHERE id=?', (rec['source_doc_id'],)).fetchone()
            
    return render_template('inventory/receipt_detail.html', receipt=rec, lines=lines, base_doc=base_doc)

@inventory_bp.route('/receipts/<int:id>/post', methods=['POST'])
@login_required
def post_receipt(id):
    conn = get_db()
    try:
        rec = conn.execute("SELECT * FROM goods_receipts WHERE id = ?", (id,)).fetchone()
        if rec['status'] != 'Draft':
            flash('Only draft receipts can be posted', 'danger')
            return redirect(url_for('inventory.receipt_detail', id=id))
            
        lines = conn.execute("SELECT * FROM goods_receipt_lines WHERE receipt_id = ?", (id,)).fetchall()
        for line in lines:
            update_stock(conn, line['item_id'], rec['warehouse_id'], line['quantity'], 0, 'Goods Receipt', id, rec['doc_ref'], line['unit_cost'], session.get('user_id'))
            
        conn.execute("UPDATE goods_receipts SET status = 'Posted', posted_at = datetime('now') WHERE id = ?", (id,))
        log_audit(conn, session.get('user_id'), 'Inventory', 'Post', 'Goods Receipt', id, f"Posted GR {rec['doc_ref']}")
        conn.commit()
        flash('Goods Receipt posted', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('inventory.receipt_detail', id=id))

# --- Goods Issues ---

@inventory_bp.route('/issues')
@login_required
def issues():
    conn = get_db()
    issues_list = conn.execute("""
        SELECT gi.*, w.name as warehouse_name 
        FROM goods_issues gi 
        LEFT JOIN warehouses w ON gi.warehouse_id = w.id 
        ORDER BY gi.id DESC
    """).fetchall()
    return render_template('inventory/issue_list.html', issues=issues_list)

@inventory_bp.route('/issues/new', methods=['GET', 'POST'])
@login_required
def new_issue():
    conn = get_db()
    if request.method == 'POST':
        warehouse_id = request.form.get('warehouse_id')
        doc_date = request.form.get('doc_date', date.today().isoformat())
        remarks = request.form.get('remarks', '')
        source_doc_id = request.form.get('source_doc_id', type=int) or None
        source_doc_type = request.form.get('source_doc_type') or None
        source_doc_ref = request.form.get('source_doc_ref') or None
        
        items = request.form.getlist('item_id[]')
        qtys = request.form.getlist('quantity[]')
        source_line_ids = request.form.getlist('source_line_id[]')
        
        try:
            doc_ref = next_ref('Inventory', 'GoodsIssue', conn)
            total = 0.0
            
            cur = conn.execute("""
                INSERT INTO goods_issues (doc_ref, doc_date, warehouse_id, status, remarks, total_value, source_doc_id, source_doc_type, source_doc_ref, posted_by)
                VALUES (?, ?, ?, 'Draft', ?, 0, ?, ?, ?, ?)
            """, (doc_ref, doc_date, warehouse_id, remarks, source_doc_id, source_doc_type, source_doc_ref, session.get('user_id')))
            iss_id = cur.lastrowid
            
            for i in range(len(items)):
                if items[i] and qtys[i]:
                    qty = float(qtys[i])
                    item = conn.execute("SELECT cost FROM items WHERE id = ?", (items[i],)).fetchone()
                    cost = item['cost'] if item else 0.0
                    line_total = qty * cost
                    total += line_total
                    s_line_id = int(source_line_ids[i]) if i < len(source_line_ids) and source_line_ids[i] else None
                    
                    conn.execute("""
                        INSERT INTO goods_issue_lines (issue_id, item_id, quantity, unit_cost, total, source_doc_type, source_doc_id, source_line_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (iss_id, items[i], qty, cost, line_total, source_doc_type, source_doc_id, s_line_id))
                    
                    if s_line_id and source_doc_type == 'Sales Order':
                        conn.execute('UPDATE sales_lines SET copied_qty = COALESCE(copied_qty, 0) + ? WHERE id = ?', (qty, s_line_id))
                    
            conn.execute("UPDATE goods_issues SET total_value = ? WHERE id = ?", (total, iss_id))
            log_audit(conn, session.get('user_id'), 'Inventory', 'Create', 'Goods Issue', iss_id, f"Created GI {doc_ref}")
            conn.commit()
            flash('Goods Issue drafted', 'success')
            return redirect(url_for('inventory.issue_detail', id=iss_id))
        except Exception as e:
            conn.rollback()
            flash(f'Error: {e}', 'danger')
            
    whs = conn.execute("SELECT * FROM warehouses WHERE is_active=1").fetchall()
    items = conn.execute("SELECT * FROM items WHERE is_stock_item=1 AND is_active=1").fetchall()
    return render_template('inventory/issue_form.html', warehouses=whs, items=items)

@inventory_bp.route('/issues/<int:id>')
@login_required
def issue_detail(id):
    conn = get_db()
    iss = conn.execute("""
        SELECT gi.*, w.name as warehouse_name 
        FROM goods_issues gi 
        LEFT JOIN warehouses w ON gi.warehouse_id = w.id 
        WHERE gi.id = ?
    """, (id,)).fetchone()
    if not iss:
        flash('Issue not found.', 'warning')
        return redirect(url_for('inventory.issues'))
        
    lines = conn.execute("SELECT gl.*, i.name as item_name, i.code as item_code FROM goods_issue_lines gl JOIN items i ON gl.item_id = i.id WHERE gl.issue_id = ?", (id,)).fetchall()
    
    base_doc = None
    if iss['source_doc_id'] and iss['source_doc_type']:
        if iss['source_doc_type'] == 'Sales Order':
            base_doc = conn.execute('SELECT id, doc_ref, doc_date, doc_type, status, total FROM sales_docs WHERE id=?', (iss['source_doc_id'],)).fetchone()
            
    return render_template('inventory/issue_detail.html', issue=iss, lines=lines, base_doc=base_doc)

@inventory_bp.route('/issues/<int:id>/post', methods=['POST'])
@login_required
def post_issue(id):
    conn = get_db()
    try:
        iss = conn.execute("SELECT * FROM goods_issues WHERE id = ?", (id,)).fetchone()
        if iss['status'] != 'Draft':
            flash('Only draft issues can be posted', 'danger')
            return redirect(url_for('inventory.issue_detail', id=id))
            
        lines = conn.execute("SELECT * FROM goods_issue_lines WHERE issue_id = ?", (id,)).fetchall()
        for line in lines:
            update_stock(conn, line['item_id'], iss['warehouse_id'], 0, line['quantity'], 'Goods Issue', id, iss['doc_ref'], line['unit_cost'], session.get('user_id'))
            
        conn.execute("UPDATE goods_issues SET status = 'Posted', posted_at = datetime('now') WHERE id = ?", (id,))
        log_audit(conn, session.get('user_id'), 'Inventory', 'Post', 'Goods Issue', id, f"Posted GI {iss['doc_ref']}")
        conn.commit()
        flash('Goods Issue posted', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('inventory.issue_detail', id=id))

# --- Transfers ---

@inventory_bp.route('/transfers')
@login_required
def transfers():
    conn = get_db()
    trans = conn.execute("""
        SELECT t.*, w1.name as from_wh, w2.name as to_wh 
        FROM stock_transfers t 
        LEFT JOIN warehouses w1 ON t.from_warehouse_id = w1.id 
        LEFT JOIN warehouses w2 ON t.to_warehouse_id = w2.id 
        ORDER BY t.id DESC
    """).fetchall()
    return render_template('inventory/transfer_list.html', transfers=trans)

@inventory_bp.route('/transfers/new', methods=['GET', 'POST'])
@login_required
def new_transfer():
    conn = get_db()
    if request.method == 'POST':
        from_wh = request.form.get('from_warehouse_id')
        to_wh = request.form.get('to_warehouse_id')
        doc_date = request.form.get('doc_date', date.today().isoformat())
        remarks = request.form.get('remarks', '')
        source_doc_id = request.form.get('source_doc_id', type=int) or None
        source_doc_type = request.form.get('source_doc_type') or None
        source_doc_ref = request.form.get('source_doc_ref') or None
        
        items = request.form.getlist('item_id[]')
        qtys = request.form.getlist('quantity[]')
        source_line_ids = request.form.getlist('source_line_id[]')
        
        if from_wh == to_wh:
            flash('Source and destination warehouse must be different.', 'error')
            return redirect(request.url)
            
        try:
            doc_ref = next_ref('Inventory', 'Transfer', conn)
            cur = conn.execute("""
                INSERT INTO stock_transfers (doc_ref, doc_date, from_warehouse_id, to_warehouse_id, status, remarks, source_doc_id, source_doc_type, source_doc_ref, posted_by)
                VALUES (?, ?, ?, ?, 'Draft', ?, ?, ?, ?, ?)
            """, (doc_ref, doc_date, from_wh, to_wh, remarks, source_doc_id, source_doc_type, source_doc_ref, session.get('user_id')))
            st_id = cur.lastrowid
            
            for i in range(len(items)):
                if items[i] and qtys[i]:
                    qty = float(qtys[i])
                    item = conn.execute("SELECT cost FROM items WHERE id = ?", (items[i],)).fetchone()
                    cost = item['cost'] if item else 0.0
                    s_line_id = int(source_line_ids[i]) if i < len(source_line_ids) and source_line_ids[i] else None
                    
                    conn.execute("""
                        INSERT INTO stock_transfer_lines (transfer_id, item_id, quantity, unit_cost, source_doc_type, source_doc_id, source_line_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (st_id, items[i], qty, cost, source_doc_type, source_doc_id, s_line_id))
                    
            log_audit(conn, session.get('user_id'), 'Inventory', 'Create', 'Stock Transfer', st_id, f"Created ST {doc_ref}")
            conn.commit()
            flash('Transfer drafted', 'success')
            return redirect(url_for('inventory.transfer_detail', id=st_id))
        except Exception as e:
            conn.rollback()
            flash(f'Error: {e}', 'danger')
            
    whs = conn.execute("SELECT * FROM warehouses WHERE is_active=1").fetchall()
    items = conn.execute("SELECT * FROM items WHERE is_stock_item=1 AND is_active=1").fetchall()
    return render_template('inventory/transfer_form.html', warehouses=whs, items=items)

@inventory_bp.route('/transfers/<int:id>')
@login_required
def transfer_detail(id):
    conn = get_db()
    trans = conn.execute("SELECT t.*, w1.name as from_wh, w2.name as to_wh FROM stock_transfers t LEFT JOIN warehouses w1 ON t.from_warehouse_id = w1.id LEFT JOIN warehouses w2 ON t.to_warehouse_id = w2.id WHERE t.id = ?", (id,)).fetchone()
    if not trans:
        flash('Transfer not found.', 'warning')
        return redirect(url_for('inventory.transfers'))
    lines = conn.execute("SELECT tl.*, i.name as item_name, i.code as item_code FROM stock_transfer_lines tl JOIN items i ON tl.item_id = i.id WHERE tl.transfer_id = ?", (id,)).fetchall()
    return render_template('inventory/transfer_detail.html', transfer=trans, lines=lines)

@inventory_bp.route('/transfers/<int:id>/post', methods=['POST'])
@login_required
def post_transfer(id):
    conn = get_db()
    try:
        trans = conn.execute("SELECT * FROM stock_transfers WHERE id = ?", (id,)).fetchone()
        if trans['status'] != 'Draft':
            flash('Only draft transfers can be posted', 'danger')
            return redirect(url_for('inventory.transfer_detail', id=id))
            
        lines = conn.execute("SELECT * FROM stock_transfer_lines WHERE transfer_id = ?", (id,)).fetchall()
        for line in lines:
            update_stock(conn, line['item_id'], trans['from_warehouse_id'], 0, line['quantity'], 'Stock Transfer', id, trans['doc_ref'], line['unit_cost'], session.get('user_id'))
            update_stock(conn, line['item_id'], trans['to_warehouse_id'], line['quantity'], 0, 'Stock Transfer', id, trans['doc_ref'], line['unit_cost'], session.get('user_id'))
            
        conn.execute("UPDATE stock_transfers SET status = 'Posted', posted_at = datetime('now') WHERE id = ?", (id,))
        log_audit(conn, session.get('user_id'), 'Inventory', 'Post', 'Stock Transfer', id, f"Posted ST {trans['doc_ref']}")
        conn.commit()
        flash('Transfer posted', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('inventory.transfer_detail', id=id))

# --- Counts ---

@inventory_bp.route('/counts')
@login_required
def counts():
    conn = get_db()
    counts_list = conn.execute("""
        SELECT ic.*, w.name as wh_name 
        FROM inventory_counts ic 
        LEFT JOIN warehouses w ON ic.warehouse_id = w.id 
        ORDER BY ic.id DESC
    """).fetchall()
    return render_template('inventory/count_list.html', counts=counts_list)

@inventory_bp.route('/counts/new', methods=['GET', 'POST'])
@login_required
def new_count():
    conn = get_db()
    if request.method == 'POST':
        warehouse_id = request.form.get('warehouse_id')
        doc_date = request.form.get('doc_date', date.today().isoformat())
        remarks = request.form.get('remarks', '')
        
        try:
            doc_ref = next_ref('Inventory', 'Count', conn)
            cur = conn.execute("""
                INSERT INTO inventory_counts (doc_ref, doc_date, warehouse_id, status, remarks, posted_by)
                VALUES (?, ?, ?, 'Draft', ?, ?)
            """, (doc_ref, doc_date, warehouse_id, remarks, session.get('user_id')))
            count_id = cur.lastrowid
            
            items = conn.execute("SELECT i.id, i.cost, COALESCE(sb.quantity, 0) as sys_qty FROM items i LEFT JOIN stock_balances sb ON i.id = sb.item_id AND sb.warehouse_id = ? WHERE i.is_stock_item = TRUE", (warehouse_id,)).fetchall()
            for item in items:
                conn.execute("INSERT INTO inventory_count_lines (count_id, item_id, system_qty, counted_qty, variance, unit_cost) VALUES (?, ?, ?, 0, ?, ?)",
                             (count_id, item['id'], item['sys_qty'], -item['sys_qty'], item['cost']))
                             
            log_audit(conn, session.get('user_id'), 'Inventory', 'Create', 'Inventory Count', count_id, f"Created IC {doc_ref}")
            conn.commit()
            flash('Inventory Count created. Enter counted quantities below.', 'success')
            return redirect(url_for('inventory.count_detail', id=count_id))
        except Exception as e:
            conn.rollback()
            flash(f'Error: {e}', 'danger')
            
    whs = conn.execute("SELECT * FROM warehouses WHERE is_active=1").fetchall()
    return render_template('inventory/count_form.html', warehouses=whs)

@inventory_bp.route('/counts/<int:id>', methods=['GET', 'POST'])
@login_required
def count_detail(id):
    conn = get_db()
    if request.method == 'POST':
        try:
            lines = conn.execute("SELECT * FROM inventory_count_lines WHERE count_id = ?", (id,)).fetchall()
            for line in lines:
                counted = request.form.get(f'counted_{line["id"]}')
                if counted is not None:
                    counted = float(counted)
                    variance = counted - line['system_qty']
                    conn.execute("UPDATE inventory_count_lines SET counted_qty = ?, variance = ? WHERE id = ?", (counted, variance, line['id']))
            conn.commit()
            flash('Quantities updated', 'success')
        except Exception as e:
            conn.rollback()
            flash(f'Error: {e}', 'danger')
            
    count = conn.execute("SELECT c.*, w.name as wh_name FROM inventory_counts c JOIN warehouses w ON c.warehouse_id = w.id WHERE c.id = ?", (id,)).fetchone()
    lines = conn.execute("SELECT cl.*, i.name as item_name, i.code as item_code FROM inventory_count_lines cl JOIN items i ON cl.item_id = i.id WHERE cl.count_id = ?", (id,)).fetchall()
    return render_template('inventory/count_detail.html', count=count, lines=lines)

@inventory_bp.route('/counts/<int:id>/post', methods=['POST'])
@login_required
def post_count(id):
    conn = get_db()
    try:
        count = conn.execute("SELECT * FROM inventory_counts WHERE id = ?", (id,)).fetchone()
        if count['status'] != 'Draft':
            flash('Only draft counts can be posted', 'danger')
            return redirect(url_for('inventory.count_detail', id=id))
            
        lines = conn.execute("SELECT * FROM inventory_count_lines WHERE count_id = ?", (id,)).fetchall()
        for line in lines:
            diff = line['variance']
            if diff > 0:
                update_stock(conn, line['item_id'], count['warehouse_id'], diff, 0, 'Inventory Count Adj', id, count['doc_ref'], line['unit_cost'], session.get('user_id'))
            elif diff < 0:
                update_stock(conn, line['item_id'], count['warehouse_id'], 0, abs(diff), 'Inventory Count Adj', id, count['doc_ref'], line['unit_cost'], session.get('user_id'))
                
        conn.execute("UPDATE inventory_counts SET status = 'Posted', posted_at = datetime('now') WHERE id = ?", (id,))
        log_audit(conn, session.get('user_id'), 'Inventory', 'Post', 'Inventory Count', id, f"Posted IC {count['doc_ref']}")
        conn.commit()
        flash('Inventory Count adjustment posted to stock balance and ledger.', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('inventory.count_detail', id=id))

# --- Reports & Exports ---

@inventory_bp.route('/reports/stock-valuation')
@login_required
def report_stock_valuation():
    conn = get_db()
    balances = conn.execute("""
        SELECT i.code, i.name, i.item_type, w.name as warehouse, sb.quantity, i.cost, (sb.quantity * i.cost) as value 
        FROM stock_balances sb 
        JOIN items i ON sb.item_id = i.id 
        JOIN warehouses w ON sb.warehouse_id = w.id
        ORDER BY w.name, i.name
    """).fetchall()
    total_val = sum(b['value'] for b in balances)
    return render_template('inventory/report_stock_valuation.html', balances=balances, data=balances, total_value=total_val)

@inventory_bp.route('/reports/low-stock')
@login_required
def report_low_stock():
    conn = get_db()
    items = conn.execute("""
        SELECT i.code, i.name, i.reorder_level, COALESCE(SUM(sb.quantity), 0) as current_stock, i.cost
        FROM items i
        LEFT JOIN stock_balances sb ON i.id = sb.item_id
        WHERE i.is_stock_item = TRUE
        GROUP BY i.id, i.code, i.name, i.reorder_level, i.cost
        HAVING COALESCE(SUM(sb.quantity), 0) <= i.reorder_level AND i.reorder_level > 0
    """).fetchall()
    return render_template('inventory/report_low_stock.html', items=items, data=items)

@inventory_bp.route('/reports/stock-ledger')
@login_required
def report_stock_ledger():
    conn = get_db()
    item_id = request.args.get('item_id', type=int)
    wh_id = request.args.get('warehouse_id', type=int)
    
    query = """
        SELECT sl.*, i.name as item_name, i.code as item_code, w.name as warehouse_name, u.username
        FROM stock_ledger sl
        JOIN items i ON sl.item_id = i.id
        JOIN warehouses w ON sl.warehouse_id = w.id
        LEFT JOIN users u ON sl.posted_by = u.id
        WHERE 1=1
    """
    params = []
    if item_id:
        query += ' AND sl.item_id = ?'
        params.append(item_id)
    if wh_id:
        query += ' AND sl.warehouse_id = ?'
        params.append(wh_id)
        
    query += ' ORDER BY sl.id DESC LIMIT 100'
    entries = conn.execute(query, params).fetchall()
    
    items = conn.execute("SELECT id, code, name FROM items WHERE is_stock_item=1").fetchall()
    whs = conn.execute("SELECT id, name FROM warehouses WHERE is_active=1").fetchall()
    return render_template('inventory/report_stock_ledger.html', entries=entries, data=entries, items=items, warehouses=whs)

# ── Price Lists & Special Prices (SAP OPLN, ITM1, SPP1) ──────────────────────
@inventory_bp.route('/price-lists')
@login_required
@require_permission('inventory', 'read')
def price_lists():
    conn = get_db()
    lists = conn.execute("""
        SELECT pl.*, 
               (SELECT COUNT(*) FROM item_prices ip WHERE ip.price_list_id = pl.id) as item_count
        FROM price_lists pl
        ORDER BY pl.id ASC
    """).fetchall()
    return render_template('inventory/price_lists.html', price_lists=lists)


@inventory_bp.route('/price-lists/new', methods=['GET', 'POST'])
@login_required
@require_permission('inventory', 'write')
def new_price_list():
    conn = get_db()
    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        name = request.form.get('name', '').strip()
        currency = request.form.get('currency', 'KES').strip()
        is_active = 1 if request.form.get('is_active') == 'on' else 0
        if not code or not name:
            flash('Code and Name are required.', 'error')
            return redirect(request.url)
        try:
            conn.execute("""
                INSERT INTO price_lists (code, name, currency, is_active, created_at)
                VALUES (?, ?, ?, ?, datetime('now'))
            """, (code, name, currency, is_active))
            conn.commit()
            log_audit(conn, session.get('user_id'), 'Inventory', 'Create', 'PriceList', None, f"Created Price List {code}")
            flash(f"Price List {name} created successfully.", "success")
            return redirect(url_for('inventory.price_lists'))
        except Exception as e:
            conn.rollback()
            flash(f"Error creating price list: {e}", 'error')
    currencies = conn.execute("SELECT code, name FROM currencies").fetchall()
    return render_template('inventory/price_list_form.html', price_list=None, currencies=currencies)


@inventory_bp.route('/price-lists/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@require_permission('inventory', 'write')
def edit_price_list(id):
    conn = get_db()
    pl = conn.execute("SELECT * FROM price_lists WHERE id = ?", (id,)).fetchone()
    if not pl:
        flash("Price List not found.", "error")
        return redirect(url_for('inventory.price_lists'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        currency = request.form.get('currency', 'KES').strip()
        is_active = 1 if request.form.get('is_active') == 'on' else 0
        try:
            conn.execute("UPDATE price_lists SET name = ?, currency = ?, is_active = ? WHERE id = ?",
                         (name, currency, is_active, id))
            conn.commit()
            log_audit(conn, session.get('user_id'), 'Inventory', 'Edit', 'PriceList', id, f"Updated Price List {pl['code']}")
            flash("Price List updated successfully.", "success")
            return redirect(url_for('inventory.price_lists'))
        except Exception as e:
            conn.rollback()
            flash(f"Error updating price list: {e}", 'error')
    currencies = conn.execute("SELECT code, name FROM currencies").fetchall()
    return render_template('inventory/price_list_form.html', price_list=pl, currencies=currencies)


@inventory_bp.route('/price-lists/<int:id>/items', methods=['GET', 'POST'])
@login_required
@require_permission('inventory', 'write')
def price_list_items(id):
    conn = get_db()
    pl = conn.execute("SELECT * FROM price_lists WHERE id = ?", (id,)).fetchone()
    if not pl:
        flash("Price List not found.", "error")
        return redirect(url_for('inventory.price_lists'))

    if request.method == 'POST':
        item_ids = request.form.getlist('item_id[]')
        prices = request.form.getlist('price[]')
        try:
            for iid_str, p_str in zip(item_ids, prices):
                if not iid_str: continue
                iid = int(iid_str)
                price_val = float(p_str) if p_str and p_str.strip() else None
                if price_val is not None:
                    existing = conn.execute("SELECT id FROM item_prices WHERE price_list_id = ? AND item_id = ?",
                                            (id, iid)).fetchone()
                    if existing:
                        conn.execute("UPDATE item_prices SET price = ? WHERE id = ?", (price_val, existing['id']))
                    else:
                        conn.execute("INSERT INTO item_prices (price_list_id, item_id, price) VALUES (?, ?, ?)",
                                     (id, iid, price_val))
                else:
                    conn.execute("DELETE FROM item_prices WHERE price_list_id = ? AND item_id = ?", (id, iid))
            conn.commit()
            log_audit(conn, session.get('user_id'), 'Inventory', 'UpdatePrices', 'PriceList', id, f"Updated prices for {pl['name']}")
            flash(f"Prices for '{pl['name']}' saved successfully.", "success")
            return redirect(url_for('inventory.price_lists'))
        except Exception as e:
            conn.rollback()
            flash(f"Error saving prices: {e}", 'error')

    items = conn.execute("""
        SELECT i.id, i.code, i.name, i.item_type, i.cost, i.price as base_price,
               ip.price as list_price
        FROM items i
        LEFT JOIN item_prices ip ON ip.item_id = i.id AND ip.price_list_id = ?
        WHERE i.is_active = 1
        ORDER BY i.code ASC
    """, (id,)).fetchall()
    return render_template('inventory/price_list_items.html', price_list=pl, items=items)


@inventory_bp.route('/special-prices')
@login_required
@require_permission('inventory', 'read')
def special_prices():
    conn = get_db()
    partner_id = request.args.get('partner_id', type=int)
    query = """
        SELECT sp.*, p.name as partner_name, p.code as partner_code,
               i.code as item_code, i.name as item_name, i.price as base_price
        FROM special_prices sp
        JOIN partners p ON sp.partner_id = p.id
        JOIN items i ON sp.item_id = i.id
        WHERE 1=1
    """
    params = []
    if partner_id:
        query += " AND sp.partner_id = ?"
        params.append(partner_id)
    query += " ORDER BY p.name ASC, i.code ASC"
    rows = conn.execute(query, params).fetchall()
    partners = conn.execute("SELECT id, code, name FROM partners ORDER BY name ASC").fetchall()
    return render_template('inventory/special_prices.html', special_prices=rows, partners=partners, current_partner_id=partner_id)


@inventory_bp.route('/special-prices/new', methods=['GET', 'POST'])
@login_required
@require_permission('inventory', 'write')
def new_special_price():
    conn = get_db()
    if request.method == 'POST':
        partner_id = request.form.get('partner_id', type=int)
        item_id = request.form.get('item_id', type=int)
        price = request.form.get('price', type=float) or 0.0
        discount_pct = request.form.get('discount_pct', type=float) or 0.0
        valid_from = request.form.get('valid_from') or None
        valid_to = request.form.get('valid_to') or None
        is_active = 1 if request.form.get('is_active') == 'on' else 0
        if not partner_id or not item_id:
            flash('Partner and Item are required.', 'error')
            return redirect(request.url)
        try:
            conn.execute("""
                INSERT INTO special_prices (partner_id, item_id, price, discount_pct, valid_from, valid_to, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (partner_id, item_id, price, discount_pct, valid_from, valid_to, is_active))
            conn.commit()
            log_audit(conn, session.get('user_id'), 'Inventory', 'Create', 'SpecialPrice', None, f"Created special price for partner {partner_id}")
            flash('Special Price created successfully.', 'success')
            return redirect(url_for('inventory.special_prices'))
        except Exception as e:
            conn.rollback()
            flash(f"Error creating special price: {e}", 'error')

    partners = conn.execute("SELECT id, code, name, partner_type FROM partners ORDER BY name ASC").fetchall()
    items = conn.execute("SELECT id, code, name, price, cost FROM items WHERE is_active = 1 ORDER BY code ASC").fetchall()
    return render_template('inventory/special_price_form.html', partners=partners, items=items)


@inventory_bp.route('/special-prices/<int:id>/delete', methods=['POST'])
@login_required
@require_permission('inventory', 'write')
def delete_special_price(id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM special_prices WHERE id = ?", (id,))
        conn.commit()
        log_audit(conn, session.get('user_id'), 'Inventory', 'Delete', 'SpecialPrice', id, f"Deleted special price {id}")
        flash('Special Price deleted.', 'success')
    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", 'error')
    return redirect(url_for('inventory.special_prices'))


@inventory_bp.route('/api/resolve-price')
@login_required
def resolve_price_api():
    """Resolve price for item given customer/partner and/or price list according to SAP hierarchy:
    1. Special Prices for Business Partner (SPP1)
    2. Price List assigned to Business Partner (ITM1 / OPLN)
    3. Item master base price / cost
    """
    item_id = request.args.get('item_id', type=int)
    partner_id = request.args.get('partner_id', type=int)
    price_list_id = request.args.get('price_list_id', type=int)
    today_str = date.today().isoformat()

    if not item_id:
        return jsonify({'error': 'item_id required'}), 400

    conn = get_db()
    item = conn.execute("SELECT id, code, name, price, cost FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return jsonify({'error': 'item not found'}), 404

    base_price = float(item['price'] or 0.0)

    # 1. Check partner special prices
    if partner_id:
        sp = conn.execute("""
            SELECT price, discount_pct FROM special_prices
            WHERE partner_id = ? AND item_id = ? AND is_active = 1
              AND (valid_from IS NULL OR valid_from <= ?)
              AND (valid_to IS NULL OR valid_to >= ?)
            ORDER BY id DESC LIMIT 1
        """, (partner_id, item_id, today_str, today_str)).fetchone()
        if sp:
            return jsonify({
                'price': float(sp['price']),
                'discount_pct': float(sp['discount_pct'] or 0.0),
                'source': 'special_price'
            })

        # If price_list_id was not provided in params, check partner's default price list
        if not price_list_id:
            p_row = conn.execute("SELECT price_list_id FROM partners WHERE id = ?", (partner_id,)).fetchone()
            if p_row and p_row['price_list_id']:
                price_list_id = p_row['price_list_id']

    # 2. Check price list
    if price_list_id:
        ip = conn.execute("""
            SELECT price FROM item_prices WHERE price_list_id = ? AND item_id = ?
        """, (price_list_id, item_id)).fetchone()
        if ip and ip['price'] is not None:
            return jsonify({
                'price': float(ip['price']),
                'discount_pct': 0.0,
                'source': 'price_list'
            })

    # 3. Base price fallback
    return jsonify({
        'price': base_price,
        'discount_pct': 0.0,
        'source': 'base'
    })

