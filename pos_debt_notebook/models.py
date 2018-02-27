# -*- coding: utf-8 -*-
from odoo import models, fields, api, SUPERUSER_ID
from datetime import datetime
from pytz import timezone
import pytz
import odoo.addons.decimal_precision as dp
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT


class ResPartner(models.Model):
    _inherit = 'res.partner'

    @api.multi
    @api.depends('report_pos_debt_ids')
    def _compute_debt(self):
        domain = [('partner_id', 'in', self.ids)]
        fields = ['partner_id', 'balance']
        res = self.env['report.pos.debt'].read_group(
            domain,
            fields,
            'partner_id')

        res_index = dict((id, {'balance': 0}) for id in self.ids)
        for data in res:
            res_index[data['partner_id'][0]] = data
        for r in self:
            r.debt = -res_index[r.id]['balance']
            r.credit_balance = -r.debt

    @api.multi
    @api.depends('report_pos_debt_ids')
    def _compute_debt_company(self):
        partners = self.filtered(lambda r: len(r.child_ids))
        if not partners:
            return
        domain = [('partner_id', 'in', partners.ids + partners.mapped('child_ids').ids)]
        fields = ['partner_id', 'balance']
        res = self.env['report.pos.debt'].read_group(
            domain,
            fields,
            'partner_id')

        res_index = dict((id, 0) for id in partners.ids)
        for data in res:
            id = data['partner_id'][0]
            balance = data['balance']
            for r in partners:
                if id == r.id or id in r.child_ids.ids:
                    res_index[r.id] += balance

        for r in partners:
            r.debt_company = -res_index[r.id]
            r.credit_balance_company = -r.debt_company

    @api.multi
    def compute_debts_by_journals(self, partner_id):
        account_journal = self.env['account.journal'].search([('debt', '=', True)])
        res = dict((id, {'balance': 0, 'journal_id': [id]}) for id in account_journal.ids)
        res_journal = self.env['report.pos.debt'].read_group(
            [('partner_id', '=', partner_id), ('journal_id', 'in', account_journal.ids)],
            ['journal_id', 'balance'],
            'journal_id')
        for d in res_journal:
            res[d['journal_id'][0]] = d
        return res

    @api.multi
    def debt_history(self, limit=0):
        """
        Get debt details

        :param int limit: max number of records to return
        :return: dictonary with keys:
             * debt: current debt
             * records_count: total count of records
             * history: list of dictionaries

                 * date
                 * config_id
                 * balance

        """
        res = []
        fields = [
            'date',
            'config_id',
            'order_id',
            'invoice_id',
            'balance',
            'product_list',
            'journal_id',
        ]
        for r in self:
            domain = [('partner_id', '=', r.id)]
            data = {"debt": r.debt}
            if limit:
                records = self.env['report.pos.debt'].search_read(
                    domain=domain,
                    fields=fields,
                    limit=limit,
                )
                for r2 in records:
                    r2['date'] = self._get_date_formats(r2['date'])
                    r2['journal_code'] = self.env['account.journal'].browse(r2['journal_id'][0]).code
                data['history'] = records
            data['debts'] = self.compute_debts_by_journals(r.id)
            data['records_count'] = self.env['report.pos.debt'].search_count(domain)
            data['partner_id'] = r.id
            res.append(data)
        return res

    debt = fields.Float(
        compute='_compute_debt', string='Debt', readonly=True,
        digits=dp.get_precision('Account'), help='Debt of this partner only.')
    credit_balance = fields.Float(
        compute='_compute_debt', string='Credit', readonly=True,
        digits=dp.get_precision('Account'), help='Credit balance of this partner only.')
    debt_company = fields.Float(
        compute='_compute_debt_company', string='Total Debt', readonly=True,
        digits=dp.get_precision('Account'), help='Debt value of this company (including its contacts)')
    credit_balance_company = fields.Float(
        compute='_compute_debt_company', string='Total Credit', readonly=True,
        digits=dp.get_precision('Account'), help='Credit balance of this company (including its contacts)')
    debt_type = fields.Selection(compute='_compute_debt_type', selection=[
        ('debt', 'Display Debt'),
        ('credit', 'Display Credit')
    ])
    report_pos_debt_ids = fields.One2many('pos.credit.update', 'partner_id',
                                          help='Technical field for proper recomputations of computed fields')

    def _get_date_formats(self, report):

        lang_code = self.env.user.lang or 'en_US'
        lang = self.env['res.lang']._lang_get(lang_code)
        date_format = lang.date_format
        time_format = lang.time_format
        fmt = date_format + " " + time_format

        server_date = datetime.strptime(report, DEFAULT_SERVER_DATETIME_FORMAT)
        utc_tz = pytz.utc.localize(server_date, is_dst=False)
        root = self.env['res.users'].browse(SUPERUSER_ID)
        user_tz = timezone(self.env.user.tz or root.tz or 'UTC')
        final = utc_tz.astimezone(user_tz)

        return final.strftime(fmt.encode('utf-8'))

    def _compute_debt_type(self):
        debt_type = self.env["ir.config_parameter"].sudo().get_param("pos_debt_notebook.debt_type", default='debt')
        for partner in self:
            partner.debt_type = debt_type

    @api.model
    def create_from_ui(self, partner):
        if partner.get('debt_limit') is False:
            del partner['debt_limit']
        return super(ResPartner, self).create_from_ui(partner)


class PosConfig(models.Model):
    _inherit = 'pos.config'

    debt_dummy_product_id = fields.Many2one(
        'product.product', string='Dummy Product for Debt', domain=[('available_in_pos', '=', True)],
        help="Dummy product used when a customer pays his debt "
             "without ordering new products. This is a workaround to the fact "
             "that Odoo needs to have at least one product on the order to "
             "validate the transaction.")

    def init_debt_journal(self):
        journal_obj = self.env['account.journal']
        user = self.env.user
        debt_journal_active = journal_obj.search([
            ('company_id', '=', user.company_id.id),
            ('debt', '=', True),
        ])
        if debt_journal_active:
            #  Check if the debt journal is created already for the company.
            return

        account_obj = self.env['account.account']
        debt_account_old_version = account_obj.search([
            ('code', '=', 'XDEBT'), ('company_id', '=', user.company_id.id)])
        if debt_account_old_version:
            debt_account = debt_account_old_version[0]
        else:
            debt_account = account_obj.create({
                'name': 'Debt',
                'code': 'XDEBT',
                'user_type_id': self.env.ref('account.data_account_type_current_assets').id,
                'company_id': user.company_id.id,
            })
            self.env['ir.model.data'].create({
                'name': 'debt_account_for_company' + str(user.company_id.id),
                'model': 'account.account',
                'module': 'pos_debt_notebook',
                'res_id': debt_account.id,
                'noupdate': True,  # If it's False, target record (res_id) will be removed while module update
            })

        default_debt_limit = 0
        if self.env['ir.module.module'].search([('name', '=', 'pos_debt_notebook')]).demo:
            self.create_demo_debt_journals(user, debt_account)
            default_debt_limit = 1000

        debt_journal_inactive = journal_obj.search([
            ('code', '=', 'TCRED'),
            ('company_id', '=', user.company_id.id),
            ('debt', '=', False),
        ])
        if debt_journal_inactive:
            debt_journal_inactive.write({
                'debt': True,
                'default_debit_account_id': debt_account.id,
                'default_credit_account_id': debt_account.id,
                'credits_via_discount': False,
                'category_ids': False,
                'write_statement': False,
                'debt_dummy_product_id': self.env.ref('pos_debt_notebook.product_pay_debt').id,
                'debt_limit': default_debt_limit,
                'pos_cash_out': True,
            })
            debt_journal = debt_journal_inactive
        else:
            debt_journal = self.create_journal({'sequence_name': 'Account Default Credit Journal ',
                                                'prefix': 'CRED ',
                                                'user': user,
                                                'noupdate': True,
                                                'journal_name': 'Credits',
                                                'code': 'TCRED',
                                                'type': 'cash',
                                                'debt': True,
                                                'journal_user': True,
                                                'debt_account': debt_account,
                                                'credits_via_discount': False,
                                                'category_ids': False,
                                                'write_statement': False,
                                                'debt_dummy_product_id': self.env.ref('pos_debt_notebook.product_pay_debt').id,
                                                'debt_limit': default_debt_limit,
                                                'pos_cash_out': True,
                                                'credits_autopay': True,
                                                })
        self.write({
            'journal_ids': [(4, debt_journal.id)],
            'debt_dummy_product_id': self.env.ref('pos_debt_notebook.product_pay_debt').id,
        })
        statement = [(0, 0, {
            'journal_id': debt_journal.id,
            'user_id': user.id,
            'company_id': user.company_id.id
        })]
        current_session = self.current_session_id
        current_session.write({
            'statement_ids': statement,
        })
        return

    def create_journal(self, vals):
        if self.env['account.journal'].search([('code', '=', vals['code'])]):
            return
        user = vals['user']
        debt_account = vals['debt_account']
        new_sequence = self.env['ir.sequence'].create({
            'name': vals['sequence_name'] + str(user.company_id.id),
            'padding': 3,
            'prefix': vals['prefix'] + str(user.company_id.id),
        })
        self.env['ir.model.data'].create({
            'name': 'journal_sequence' + str(new_sequence.id),
            'model': 'ir.sequence',
            'module': 'pos_debt_notebook',
            'res_id': new_sequence.id,
            'noupdate': vals['noupdate'],  # If it's False, target record (res_id) will be removed while module update
        })
        debt_journal = self.env['account.journal'].create({
            'name': vals['journal_name'],
            'code': vals['code'],
            'type': vals['type'],
            'debt': vals['debt'],
            'journal_user': vals['journal_user'],
            'sequence_id': new_sequence.id,
            'company_id': user.company_id.id,
            'default_debit_account_id': debt_account.id,
            'default_credit_account_id': debt_account.id,
            'debt_limit': vals['debt_limit'],
            'category_ids': vals['category_ids'],
            'pos_cash_out': vals['pos_cash_out'],
            'credits_via_discount': vals['credits_via_discount'],
            'credits_autopay': vals['credits_autopay'],
        })
        self.env['ir.model.data'].create({
            'name': 'debt_journal_' + str(debt_journal.id),
            'model': 'account.journal',
            'module': 'pos_debt_notebook',
            'res_id': int(debt_journal.id),
            'noupdate': True,  # If it's False, target record (res_id) will be removed while module update
        })
        if vals['write_statement']:
            self.write({
                'journal_ids': [(4, debt_journal.id)],
                'debt_dummy_product_id': vals['debt_dummy_product_id'],
            })
            statement = [(0, 0, {
                'journal_id': debt_journal.id,
                'user_id': user.id,
                'company_id': user.company_id.id
            })]
            current_session = self.current_session_id
            current_session.write({
                'statement_ids': statement,
            })
        return debt_journal

    @api.multi
    def open_session_cb(self):
        res = super(PosConfig, self).open_session_cb()
        self.init_debt_journal()
        return res

    def create_demo_debt_journals(self, user, debt_account):
        self.create_journal({'sequence_name': 'Account Default Credit via Discounts Journal ',
                             'prefix': 'CRD ',
                             'user': user,
                             'noupdate': True,
                             'journal_name': 'Credits (via discounts)',
                             'code': 'DCRD',
                             'type': 'cash',
                             'debt': True,
                             'journal_user': True,
                             'debt_account': debt_account,
                             'credits_via_discount': True,
                             'category_ids': False,
                             'write_statement': True,
                             'debt_dummy_product_id': False,
                             'debt_limit': 1000,
                             'pos_cash_out': False,
                             'credits_autopay': True,
                             })
        allowed_category = self.env.ref('point_of_sale.fruits_vegetables').id
        self.create_journal({'sequence_name': 'Account Default Credit Journal F&V',
                             'prefix': 'CRD ',
                             'user': user,
                             'noupdate': True,
                             'journal_name': 'Credits (Fruits & Vegetables only)',
                             'code': 'FCRD',
                             'type': 'cash',
                             'debt': True,
                             'journal_user': True,
                             'debt_account': debt_account,
                             'credits_via_discount': False,
                             'category_ids': [(6, 0, [allowed_category])],
                             'write_statement': True,
                             'debt_dummy_product_id': False,
                             'debt_limit': 1000,
                             'pos_cash_out': False,
                             'credits_autopay': True,
                             })


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    debt = fields.Boolean(string='Credit Journal')
    pos_cash_out = fields.Boolean(string='Allow to cash out credits', default=False,
                                  help='Partner can exchange credits to cash in POS')
    category_ids = fields.Many2many('pos.category', string='POS product categories',
                                    help='POS product categories that can be paid with this credits.'
                                         'If the field is empty then all categories may be purchased with this journal')
    debt_limit = fields.Float(string='Max Debt', digits=dp.get_precision('Account'), default=0,
                              help='Partners is not allowed to have a debt more than this value')
    credits_via_discount = fields.Boolean(
        default=False, string='Zero transactions on credit payments',
        help='Discount the order (mostly 100%) when user pay via this type of credits')
    credits_autopay = fields.Boolean("Autopay", default=True,
                                     help="On payment screen it will be automatically used if balance is positive. "
                                          "In case of several autopay journals they will be applied in Journal order until full amount is paid")

    @api.onchange('credits_via_discount')
    def _onchange_partner(self):
        if self.credits_via_discount is True:
            self.pos_cash_out = False


class PosConfiguration(models.TransientModel):
    _inherit = 'pos.config.settings'

    debt_type = fields.Selection([
        ('debt', 'Display Debt'),
        ('credit', 'Display Credit')
    ], default='debt', string='Debt Type', help='Way to display debt value (label and sign of the amount). '
                                                'In both cases debt will be red, credit - green')
    debt_limit = fields.Float(
        string='Default Max Debt', digits=dp.get_precision('Account'), default=0,
        help='Default value for new Customers')

    @api.multi
    def set_debt_type(self):
        self.env["ir.config_parameter"].set_param("pos_debt_notebook.debt_type", self.debt_type)

    @api.multi
    def get_default_debt_type(self, fields):
        debt_type = self.env["ir.config_parameter"].get_param("pos_debt_notebook.debt_type", default='debt')
        return {'debt_type': debt_type}

    @api.multi
    def set_debt_limit(self):
        self.env["ir.config_parameter"].set_param("pos_debt_notebook.debt_limit", str(self.debt_limit))

    @api.multi
    def get_default_debt_limit(self, fields):
        debt_limit = self.env["ir.config_parameter"].get_param("pos_debt_notebook.debt_limit", default=0)
        return {'debt_limit': debt_limit}


class Product(models.Model):

    _inherit = 'product.template'

    credit_product = fields.Many2one('account.journal', string='Journal Credit Product',
                                     help="This product is used to buy Credits (pay for debts).")


class PosOrder(models.Model):
    _inherit = "pos.order"

    product_list = fields.Text('Product list', compute='_compute_product_list', store=True)
    pos_credit_update_ids = fields.One2many('pos.credit.update', 'order_id', string='Non-Accounting Payments')

    @api.multi
    @api.depends('lines', 'lines.product_id', 'lines.product_id.name', 'lines.qty', 'lines.price_unit')
    def _compute_product_list(self):
        for order in self:
            product_list = list()
            for o_line in order.lines:
                product_list.append('%s(%s * %s) + ' % (o_line.product_id.name, o_line.qty, o_line.price_unit))
            order.product_list = ''.join(product_list).strip(' + ')

    @api.model
    def _process_order(self, pos_order):
        credit_updates = []
        for payment in pos_order['statement_ids']:
            journal = self.env['account.journal'].browse(payment[2]['journal_id'])
            if journal.credits_via_discount:
                amount = float(payment[2]['amount'])
                product_list = list()
                for o_line in pos_order['lines']:
                    o_line = o_line[2]
                    name = self.env['product.product'].browse(o_line['product_id']).name
                    product_list.append('%s(%s * %s) + ' % (name, o_line['qty'], o_line['price_unit']))
                product_list = ''.join(product_list).strip(' + ')
                credit_updates.append({'journal_id': payment[2]['journal_id'],
                                       'balance': -amount,
                                       'partner_id': pos_order['partner_id'],
                                       'update_type': 'balance_update',
                                       'note': product_list,
                                       })
                payment[2]['amount'] = 0
        pos_order['amount_return'] -= pos_order.get('amount_via_discount', 0)
        order = super(PosOrder, self)._process_order(pos_order)
        for update in credit_updates:
            update['order_id'] = order.id
            entry = self.env['pos.credit.update'].sudo().create(update)
            entry.switch_to_confirm()
        return order


class PosCreditUpdate(models.Model):
    _name = 'pos.credit.update'
    _description = "Manual Credit Updates"
    _inherit = ['mail.thread']
    _order = 'id desc'

    partner_id = fields.Many2one('res.partner', string="Partner", required=True, track_visibility='always')
    user_id = fields.Many2one(
        'res.users',
        string='Salesperson',
        default=lambda s: s.env.user,
        readonly=True
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        default=lambda s: s.env.user.company_id,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        default=lambda s: s.env.user.company_id.currency_id,
    )
    balance = fields.Monetary('Balance Update', track_visibility='always',
                              help="Change of balance. Negative value for purchases without money (debt). Positive for credit payments (prepament or payments for debts).")
    new_balance = fields.Monetary('New Balance', help="Value to set balance to. Used only in Draft state.")
    note = fields.Text('Note')
    date = fields.Datetime(string='Date', default=fields.Datetime.now, required=True)

    state = fields.Selection([
        ('draft', 'Draft'),
        ('confirm', 'Confirmed'),
        ('cancel', 'Canceled')
    ], default='draft', required=True, track_visibility='always')
    update_type = fields.Selection([('balance_update', 'Balance Update'), ('new_balance', 'New Balance')], default='balance_update', required=True)
    journal_id = fields.Many2one('account.journal', string='Journal', required=True, domain="[('debt', '=', True)]")
    order_id = fields.Many2one('pos.order', string="POS Order")

    def get_balance(self_, balance, new_balance):
        return -balance + new_balance

    def update_balance(self, vals):
        partner_id = vals.get('partner_id', self.partner_id.id)
        new_balance = vals.get('new_balance', self.new_balance)
        state = vals.get('state', self.state) or 'draft'
        update_type = vals.get('update_type', self.update_type)
        if (state == 'draft' and update_type == 'new_balance'):
            credit_balance = self.partner_id.browse(partner_id).credit_balance
            vals['balance'] = self.get_balance(credit_balance, new_balance)

    @api.model
    def create(self, vals):
        self.update_balance(vals)
        return super(PosCreditUpdate, self).create(vals)

    @api.multi
    def write(self, vals):
        self.update_balance(vals)
        return super(PosCreditUpdate, self).write(vals)

    def switch_to_confirm(self):
        self.write({'state': 'confirm'})

    def switch_to_cancel(self):
        self.write({'state': 'cancel'})

    def switch_to_draft(self):
        self.write({'state': 'draft'})

    def do_confirm(self):
        active_ids = self._context.get('active_ids')
        for r in self.env['pos.credit.update'].browse(active_ids):
            r.switch_to_confirm()
