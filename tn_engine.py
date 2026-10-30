"""
tn_engine.py — SAP Business One Transaction Notification & Business Rules Engine
Ported from SAP B1 SBO_SP_TransactionNotification SQL implementations.

Provides enterprise-grade governance:
  1. TN: Prevent Sale Below Cost (TN_Prevent_Sale_Below_Cost.sql)
  2. TN: Business Partner Validation & Credit Limit Enforcement (Business_Partner_Validation_TN.sql)
  3. TN: Stock Availability / Negative Stock Prevention
  4. Simulation Engine for SAP B1 Journal Entry Preview
  5. Relationship Map Graph Builder (Document Flow Tree)
"""

import re
from datetime import datetime

# ==============================================================================
# 1. SAP B1 Transaction Notification Validators
# ==============================================================================

class TransactionNotificationError(Exception):
    """Raised when a business rule or transaction notification is violated."""
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"SAP B1 Validation [{code}]: {message}")


def validate_sale_lines_not_below_cost(conn, doc_type, items_lines):
    """
    Replicates SAP B1 TN: Prevent Sale of Items Below Item Cost (TN_Prevent_Sale_Below_Cost.sql).
    Checks Quotations, Sales Orders, Deliveries, and Invoices.
    If the effective selling price per unit < item cost, raises TransactionNotificationError.
    """
    if doc_type not in ('Quotation', 'Sales Order', 'Delivery', 'Invoice'):
        return

    for line in items_lines:
        item_id = line.get('item_id')
        qty = float(line.get('quantity') or 0.0)
        unit_price = float(line.get('unit_price') or 0.0)
        disc_pct = float(line.get('discount_pct') or 0.0)

        if not item_id or qty <= 0:
            continue

        item = conn.execute("SELECT code, name, cost, is_stock_item FROM items WHERE id = ?", (item_id,)).fetchone()
        if not item or not item['cost'] or float(item['cost']) <= 0:
            continue

        item_cost = float(item['cost'])
        # Effective unit selling price after discount
        eff_price = unit_price * (1.0 - disc_pct / 100.0)

        # Disallow selling below cost (threshold of 0.001 to prevent floating point noise)
        if eff_price < (item_cost - 0.001):
            raise TransactionNotificationError(
                code="17001",
                message=(
                    f"Item [{item['code']}] '{item['name']}' cannot be sold below its cost. "
                    f"Effective Selling Price (KES {eff_price:,.2f}) is lower than Item Cost (KES {item_cost:,.2f})."
                )
            )


def validate_partner_credit_limit(conn, partner_id, new_amount, doc_type="Sales Order"):
    """
    Replicates SAP B1 TN: Credit Limit Verification on Sales Orders and Invoices.
    Checks if Customer balance + new transaction total > credit_limit.
    """
    if not partner_id or new_amount <= 0:
        return

    partner = conn.execute(
        "SELECT code, name, balance, credit_limit, status FROM partners WHERE id = ?", 
        (partner_id,)
    ).fetchone()

    if not partner:
        return

    if partner['status'] in ('Inactive', 'Blocked'):
        raise TransactionNotificationError(
            code="10001",
            message=f"Business Partner [{partner['code']}] '{partner['name']}' is {partner['status']} and cannot be used."
        )

    credit_limit = float(partner['credit_limit'] or 0.0)
    current_balance = float(partner['balance'] or 0.0)

    if credit_limit > 0 and (current_balance + new_amount) > credit_limit:
        excess = (current_balance + new_amount) - credit_limit
        raise TransactionNotificationError(
            code="17002",
            message=(
                f"Credit limit exceeded for Customer [{partner['code']}] '{partner['name']}'. "
                f"Current Balance: KES {current_balance:,.2f}, New Transaction: KES {new_amount:,.2f}, "
                f"Projected Balance: KES {current_balance + new_amount:,.2f} exceeds Credit Limit of KES {credit_limit:,.2f} "
                f"by KES {excess:,.2f}."
            )
        )


def validate_stock_availability(conn, warehouse_id, items_lines, doc_type="Delivery"):
    """
    Replicates SAP B1 TN: Prevent Negative Inventory.
    Validates that on Deliveries, Goods Issues, or Direct Invoices, stock is sufficient on a row-by-row warehouse basis.
    """
    whs_cache = {}

    for line in items_lines:
        item_id = line.get('item_id')
        qty = float(line.get('quantity') or 0.0)
        if not item_id or qty <= 0:
            continue

        line_whs = line.get('warehouse_id') or warehouse_id
        if not line_whs:
            continue

        if line_whs not in whs_cache:
            w_row = conn.execute("SELECT code, name FROM warehouses WHERE id = ?", (line_whs,)).fetchone()
            whs_cache[line_whs] = w_row['name'] if w_row else f"Warehouse #{line_whs}"
        whs_name = whs_cache[line_whs]

        item = conn.execute("SELECT code, name, is_stock_item FROM items WHERE id = ?", (item_id,)).fetchone()
        if not item or not item['is_stock_item']:
            continue

        bal_row = conn.execute(
            "SELECT quantity FROM stock_balances WHERE item_id = ? AND warehouse_id = ?",
            (item_id, line_whs)
        ).fetchone()

        curr_stock = float(bal_row['quantity']) if bal_row and bal_row['quantity'] is not None else 0.0

        if curr_stock < qty:
            raise TransactionNotificationError(
                code="15001",
                message=(
                    f"Insufficient stock for [{item['code']}] '{item['name']}' in {whs_name}. "
                    f"Available on hand: {curr_stock:,.2f}, Required: {qty:,.2f}."
                )
            )


# ==============================================================================
# 2. SAP B1 Journal Entry Preview Simulator
# ==============================================================================

def simulate_journal_entry(conn, doc_type, doc_data, lines):
    """
    Simulates the General Ledger postings for a document before posting (SAP B1 Journal Entry Preview).
    Returns dict:
      {
        'doc_type': doc_type,
        'doc_ref': doc_ref,
        'doc_date': doc_date,
        'balanced': True/False,
        'total_debit': float,
        'total_credit': float,
        'lines': [
            {'account_code': '1200', 'account_name': 'Accounts Receivable', 'debit': 1500.0, 'credit': 0.0, 'memo': '...'},
            ...
        ]
      }
    """
    coa_rows = conn.execute("SELECT id, code, name FROM coa").fetchall()
    coa_by_code = {c['code']: c for c in coa_rows}

    sim_lines = []
    total_debit = 0.0
    total_credit = 0.0

    doc_ref = doc_data.get('doc_ref', 'PREVIEW')
    doc_date = doc_data.get('doc_date', datetime.today().strftime('%Y-%m-%d'))
    total = float(doc_data.get('total') or 0.0)
    subtotal = float(doc_data.get('subtotal') or total)
    tax_total = float(doc_data.get('tax_total') or 0.0)

    if doc_type == 'Invoice':
        # AR Account (Debit)
        ar = coa_by_code.get('1200', {'code': '1200', 'name': 'Accounts Receivable'})
        sim_lines.append({
            'account_code': ar['code'],
            'account_name': ar['name'],
            'debit': total,
            'credit': 0.0,
            'memo': f"Customer Receivable — {doc_ref}"
        })
        total_debit += total

        # Revenue Account (Credit)
        rev = coa_by_code.get('4100', {'code': '4100', 'name': 'Sales Revenue'})
        sim_lines.append({
            'account_code': rev['code'],
            'account_name': rev['name'],
            'debit': 0.0,
            'credit': subtotal,
            'memo': f"Sales Revenue — {doc_ref}"
        })
        total_credit += subtotal

        # Tax Account (Credit) if applicable
        if tax_total > 0:
            tax = coa_by_code.get('2210', {'code': '2210', 'name': 'VAT Output Tax Payable'})
            sim_lines.append({
                'account_code': tax['code'],
                'account_name': tax['name'],
                'debit': 0.0,
                'credit': tax_total,
                'memo': f"Output Tax — {doc_ref}"
            })
            total_credit += tax_total

        # Check if inventory cost also moved (direct invoice not from delivery)
        if doc_data.get('source_doc_type') != 'Delivery':
            cost_total = 0.0
            for l in lines:
                item_id = l.get('item_id')
                qty = float(l.get('quantity') or 0.0)
                if item_id:
                    it = conn.execute("SELECT cost, is_stock_item FROM items WHERE id = ?", (item_id,)).fetchone()
                    if it and it['is_stock_item'] and it['cost']:
                        cost_total += qty * float(it['cost'])
            if cost_total > 0:
                cogs = coa_by_code.get('5100', {'code': '5100', 'name': 'Cost of Goods Sold'})
                inv = coa_by_code.get('1300', {'code': '1300', 'name': 'Inventory Finished Goods'})
                sim_lines.append({
                    'account_code': cogs['code'],
                    'account_name': cogs['name'],
                    'debit': cost_total,
                    'credit': 0.0,
                    'memo': f"COGS Allocation — {doc_ref}"
                })
                sim_lines.append({
                    'account_code': inv['code'],
                    'account_name': inv['name'],
                    'debit': 0.0,
                    'credit': cost_total,
                    'memo': f"Inventory Reduction — {doc_ref}"
                })
                total_debit += cost_total
                total_credit += cost_total

    elif doc_type == 'Delivery':
        # Delivery moves inventory to COGS
        cost_total = 0.0
        for l in lines:
            item_id = l.get('item_id')
            qty = float(l.get('quantity') or 0.0)
            if item_id:
                it = conn.execute("SELECT cost, is_stock_item FROM items WHERE id = ?", (item_id,)).fetchone()
                if it and it['is_stock_item'] and it['cost']:
                    cost_total += qty * float(it['cost'])
        if cost_total > 0:
            cogs = coa_by_code.get('5100', {'code': '5100', 'name': 'Cost of Goods Sold'})
            inv = coa_by_code.get('1300', {'code': '1300', 'name': 'Inventory Finished Goods'})
            sim_lines.append({
                'account_code': cogs['code'],
                'account_name': cogs['name'],
                'debit': cost_total,
                'credit': 0.0,
                'memo': f"COGS on Dispatch — {doc_ref}"
            })
            sim_lines.append({
                'account_code': inv['code'],
                'account_name': inv['name'],
                'debit': 0.0,
                'credit': cost_total,
                'memo': f"Inventory Reduction on Delivery — {doc_ref}"
            })
            total_debit += cost_total
            total_credit += cost_total

    elif doc_type in ('Bill', 'Purchase Invoice'):
        # Expense / Inventory (Debit)
        inv = coa_by_code.get('1300', {'code': '1300', 'name': 'Inventory Purchases'})
        sim_lines.append({
            'account_code': inv['code'],
            'account_name': inv['name'],
            'debit': subtotal,
            'credit': 0.0,
            'memo': f"Supplier Purchase — {doc_ref}"
        })
        total_debit += subtotal

        # Input Tax (Debit) if applicable
        if tax_total > 0:
            tax = coa_by_code.get('2220', {'code': '2220', 'name': 'VAT Input Tax Recoverable'})
            sim_lines.append({
                'account_code': tax['code'],
                'account_name': tax['name'],
                'debit': tax_total,
                'credit': 0.0,
                'memo': f"Input Tax — {doc_ref}"
            })
            total_debit += tax_total

        # AP Account (Credit)
        ap = coa_by_code.get('2100', {'code': '2100', 'name': 'Accounts Payable'})
        sim_lines.append({
            'account_code': ap['code'],
            'account_name': ap['name'],
            'debit': 0.0,
            'credit': total,
            'memo': f"Supplier Payable — {doc_ref}"
        })
        total_credit += total

    balanced = abs(total_debit - total_credit) < 0.005

    return {
        'doc_type': doc_type,
        'doc_ref': doc_ref,
        'doc_date': doc_date,
        'balanced': balanced,
        'total_debit': total_debit,
        'total_credit': total_credit,
        'lines': sim_lines
    }


# ==============================================================================
# 3. SAP B1 & ERPNext Visual Relationship Map (Document Flow Tree)
# ==============================================================================

def build_relationship_map(conn, module, doc_id):
    """
    Builds the complete multi-tier visual relationship tree for a document.
    Traces:
      - Ancestor documents (e.g. Quotation -> Sales Order -> Delivery)
      - Current document
      - Descendant documents (e.g. Delivery -> Invoice -> Credit Memo)
      - Linked Payments (Incoming or Outgoing Payments)
      - Linked Journal Entries
    Returns a structured graph of nodes and connections for visualization.
    """
    nodes = []
    edges = []
    seen_ids = set()

    def doc_kind_for(dt):
        mapping = {
            'Quotation': 'quotations',
            'Sales Order': 'orders',
            'Delivery': 'deliveries',
            'Invoice': 'invoices',
            'Down Payment Invoice': 'down_payments',
            'Sales Return': 'returns',
            'Purchase Order': 'orders',
            'Goods Receipt PO': 'grpo',
            'Bill': 'bills'
        }
        return mapping.get(dt, 'invoices')

    # 1. Fetch current document
    table = 'sales_docs' if module == 'sales' else 'purchase_docs'
    cur = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (doc_id,)).fetchone()
    if not cur:
        return {'nodes': [], 'edges': []}

    cur_node_id = f"{module}_{cur['id']}"
    nodes.append({
        'id': cur_node_id,
        'doc_id': cur['id'],
        'doc_type': cur['doc_type'],
        'doc_ref': cur['doc_ref'],
        'doc_date': cur['doc_date'],
        'status': cur['status'],
        'total': float(cur['total'] or 0.0),
        'balance_due': float(cur['balance_due'] or 0.0),
        'is_current': True,
        'module': module,
        'url': f"/{module}/{doc_kind_for(cur['doc_type'])}/{cur['id']}"
    })
    seen_ids.add(cur_node_id)

    # 2. Walk up to find ancestors (Base Documents)
    curr_ancestor_id = cur['source_doc_id']
    curr_ancestor_type = cur['source_doc_type']
    child_node_id = cur_node_id

    while curr_ancestor_id:
        parent = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (curr_ancestor_id,)).fetchone()
        if not parent:
            break

        parent_node_id = f"{module}_{parent['id']}"
        if parent_node_id not in seen_ids:
            nodes.insert(0, {
                'id': parent_node_id,
                'doc_id': parent['id'],
                'doc_type': parent['doc_type'],
                'doc_ref': parent['doc_ref'],
                'doc_date': parent['doc_date'],
                'status': parent['status'],
                'total': float(parent['total'] or 0.0),
                'balance_due': float(parent['balance_due'] or 0.0),
                'is_current': False,
                'module': module,
                'url': f"/{module}/{doc_kind_for(parent['doc_type'])}/{parent['id']}"
            })
            seen_ids.add(parent_node_id)

        edges.append({'from': parent_node_id, 'to': child_node_id, 'label': 'Based On'})
        child_node_id = parent_node_id
        curr_ancestor_id = parent['source_doc_id']

    # 3. Find descendants (Target Documents copied from this document)
    lines_table = 'sales_lines' if module == 'sales' else 'purchase_lines'
    descendants = conn.execute(f"""
        SELECT DISTINCT d.* 
        FROM {table} d
        LEFT JOIN {lines_table} l ON l.doc_id = d.id
        WHERE d.source_doc_id = ? OR l.source_doc_id = ?
        ORDER BY d.id ASC
    """, (cur['id'], cur['id'])).fetchall()

    for d in descendants:
        desc_node_id = f"{module}_{d['id']}"
        if desc_node_id not in seen_ids:
            nodes.append({
                'id': desc_node_id,
                'doc_id': d['id'],
                'doc_type': d['doc_type'],
                'doc_ref': d['doc_ref'],
                'doc_date': d['doc_date'],
                'status': d['status'],
                'total': float(d['total'] or 0.0),
                'balance_due': float(d['balance_due'] or 0.0),
                'is_current': False,
                'module': module,
                'url': f"/{module}/{doc_kind_for(d['doc_type'])}/{d['id']}"
            })
            seen_ids.add(desc_node_id)
        edges.append({'from': cur_node_id, 'to': desc_node_id, 'label': 'Copied To'})

    # 4. Find linked Payments
    if cur['doc_type'] == 'Invoice' or cur['doc_type'] == 'Down Payment Invoice':
        try:
            allocations = conn.execute("""
                SELECT p.id, p.doc_ref, p.doc_date, p.payment_type, p.amount, p.status, pa.amount as allocated_amount
                FROM payment_allocations pa
                JOIN payments p ON pa.payment_id = p.id
                WHERE pa.doc_type IN ('Invoice', 'sales_docs') AND pa.doc_id = ?
            """, (cur['id'],)).fetchall()

            for pay in allocations:
                pay_node_id = f"payment_{pay['id']}"
                if pay_node_id not in seen_ids:
                    nodes.append({
                        'id': pay_node_id,
                        'doc_id': pay['id'],
                        'doc_type': f"{pay['payment_type']} Payment",
                        'doc_ref': pay['doc_ref'],
                        'doc_date': pay['doc_date'],
                        'status': pay['status'] or 'Posted',
                        'total': float(pay['amount'] or 0.0),
                        'balance_due': 0.0,
                        'is_current': False,
                        'module': 'payments',
                        'url': f"/payments/{pay['id']}"
                    })
                    seen_ids.add(pay_node_id)
                edges.append({'from': cur_node_id, 'to': pay_node_id, 'label': f"Paid KES {float(pay['allocated_amount']):,.2f}"})
        except Exception as e:
            print(f"[RelationshipMap] Note on payments: {e}")

    # 5. Find linked Journal Entries
    try:
        jes = conn.execute("""
            SELECT id, doc_ref, doc_date, description, status 
            FROM journal_entries 
            WHERE source_doc_id = ? AND (source_doc_type = ? OR source_doc_type = 'Sales' OR source_doc_type = 'Invoice')
        """, (cur['id'], cur['doc_type'])).fetchall()

        for je in jes:
            je_node_id = f"je_{je['id']}"
            if je_node_id not in seen_ids:
                nodes.append({
                    'id': je_node_id,
                    'doc_id': je['id'],
                    'doc_type': 'Journal Entry',
                    'doc_ref': je['doc_ref'],
                    'doc_date': je['doc_date'],
                    'status': je['status'] or 'Posted',
                    'total': 0.0,
                    'balance_due': 0.0,
                    'is_current': False,
                    'module': 'finance',
                    'url': f"/finance/journals/{je['id']}"
                })
                seen_ids.add(je_node_id)
            edges.append({'from': cur_node_id, 'to': je_node_id, 'label': 'GL Posting'})
    except Exception as e:
        print(f"[RelationshipMap] Note on journals: {e}")

    return {'nodes': nodes, 'edges': edges}
