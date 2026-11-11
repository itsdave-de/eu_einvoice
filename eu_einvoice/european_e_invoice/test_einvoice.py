from unittest.mock import patch

import frappe
from drafthorse.models.document import IncludedNote
from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days

from eu_einvoice.european_e_invoice.custom.sales_invoice import SalesInvoiceGenerator
from eu_einvoice.european_e_invoice.einvoice import attach_xml_to_pdf, get_einvoice, get_generator_class
from eu_einvoice.utils import EInvoiceProfile

HOOK_PATH = "eu_einvoice.european_e_invoice.test_einvoice"


def before_einvoice_generation(doc, method=None):
	"""Like doc_events in other apps: change the invoice before it is read."""
	doc.po_no = "PO-FROM-BEFORE-HOOK"


def after_einvoice_generation(doc, method=None, einvoice_doc=None):
	"""Like doc_events in other apps: patch the drafthorse document before it is serialized."""
	note = IncludedNote(subject_code="AAI")
	note.content = f"Added by hook for {doc.name}"
	einvoice_doc.header.notes.add(note)


class TestEInvoiceHooks(IntegrationTestCase):
	def test_doc_events_before_and_after_generation(self):
		invoice = create_sales_invoice(do_not_save=True)
		invoice.einvoice_profile = EInvoiceProfile.EN16931.value
		invoice.insert()

		doc_events = {
			"Sales Invoice": {
				"before_einvoice_generation": [f"{HOOK_PATH}.before_einvoice_generation"],
				"after_einvoice_generation": [f"{HOOK_PATH}.after_einvoice_generation"],
			}
		}
		with patch("frappe.get_doc_hooks", return_value=doc_events):
			xml = get_einvoice(invoice).decode()

		self.assertIn("PO-FROM-BEFORE-HOOK", xml)
		self.assertIn(f"Added by hook for {invoice.name}", xml)


class TestGeneratorRegistry(IntegrationTestCase):
	def test_sales_invoice_is_registered(self):
		self.assertIs(get_generator_class("Sales Invoice"), SalesInvoiceGenerator)

	def test_unregistered_doctype(self):
		self.assertIsNone(get_generator_class("ToDo"))

	def test_last_registered_app_wins(self):
		hooks = {
			"Sales Invoice": [
				"eu_einvoice.european_e_invoice.einvoice.EInvoiceGenerator",
				"eu_einvoice.european_e_invoice.custom.sales_invoice.SalesInvoiceGenerator",
			]
		}
		with patch("frappe.get_hooks", return_value=hooks):
			self.assertIs(get_generator_class("Sales Invoice"), SalesInvoiceGenerator)

	def test_get_einvoice_rejects_unregistered_doctype(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "Not an invoice"})
		self.assertRaises(frappe.ValidationError, get_einvoice, todo)

	def test_attach_xml_to_pdf_keeps_pdf_of_unregistered_doctype(self):
		pdf_data = b"%PDF-1.4 not an invoice"
		self.assertIs(attach_xml_to_pdf("TODO-0001", pdf_data, doctype="ToDo"), pdf_data)


class TestSkontoTerms(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.template = "_Test Skonto 14 Days 2 Percent"
		if frappe.db.exists("Payment Terms Template", cls.template):
			return

		frappe.get_doc(
			doctype="Payment Term",
			payment_term_name=cls.template,
			invoice_portion=100,
			due_date_based_on="Day(s) after invoice date",
			credit_days=30,
			discount_type="Percentage",
			discount=2,
			discount_validity_based_on="Day(s) after invoice date",
			discount_validity=14,
		).insert()
		frappe.get_doc(
			doctype="Payment Terms Template",
			template_name=cls.template,
			terms=[{"payment_term": cls.template, "invoice_portion": 100}],
		).insert()

	def _get_xml(self, payment_terms_template=None, discount_date_offset=None) -> str:
		invoice = create_sales_invoice(do_not_save=True)
		invoice.einvoice_profile = EInvoiceProfile.XRECHNUNG.value
		invoice.payment_terms_template = payment_terms_template
		invoice.insert()
		if discount_date_offset is not None:
			invoice.payment_schedule[0].discount_date = add_days(invoice.posting_date, discount_date_offset)

		return get_einvoice(invoice).decode()

	def test_skonto_line_for_discount_after_posting_date(self):
		self.assertIn("#SKONTO#TAGE=14#PROZENT=2.00#", self._get_xml(self.template))

	def test_no_skonto_line_for_discount_before_posting_date(self):
		self.assertNotIn("#SKONTO#", self._get_xml(self.template, discount_date_offset=-1))

	def test_no_skonto_line_without_discount(self):
		self.assertNotIn("#SKONTO#", self._get_xml())


EINVOICE = "eu_einvoice.european_e_invoice.einvoice"


def _fake_attach_print(doctype, name, *args, **kwargs):
	return {"fname": f"{name}.pdf", "fcontent": b"%PDF-plain"}


class TestAttachPrint(IntegrationTestCase):
	"""ZUGFeRD XML in PDFs rendered by frappe.attach_print, e.g. email attachments (#274, #329)."""

	def _patched(self, after_print_hook=False, **attach_xml):
		from contextlib import ExitStack

		from frappe.utils import print_utils

		stack = ExitStack()
		stack.enter_context(patch.object(print_utils, "attach_print", _fake_attach_print))
		stack.enter_context(patch.object(frappe, "attach_print", _fake_attach_print))
		stack.enter_context(patch(f"{EINVOICE}.has_after_print_hook", return_value=after_print_hook))
		mock = stack.enter_context(patch(f"{EINVOICE}.attach_xml_to_pdf", **attach_xml))
		return stack, mock

	def test_embeds_xml_for_invoices_only(self):
		from eu_einvoice.european_e_invoice.einvoice import patch_attach_print

		stack, attach_xml = self._patched(return_value=b"%PDF-zugferd")
		with stack:
			patch_attach_print()
			patch_attach_print()
			self.assertTrue(frappe.attach_print._eu_einvoice)
			self.assertIs(frappe.attach_print.__wrapped__, _fake_attach_print)
			invoice = frappe.attach_print(doctype="Sales Invoice", name="ACC-SINV-TEST-1", print_format="Standard")
			other = frappe.attach_print("Quotation", "SAL-QTN-TEST-1")

		self.assertEqual(invoice["fcontent"], b"%PDF-zugferd")
		self.assertEqual(other["fcontent"], b"%PDF-plain")
		attach_xml.assert_called_once_with("ACC-SINV-TEST-1", b"%PDF-plain", "Sales Invoice")

	def test_keeps_pdf_if_xml_fails(self):
		from eu_einvoice.european_e_invoice.einvoice import patch_attach_print

		stack, _ = self._patched(side_effect=Exception("broken"))
		with stack, patch("frappe.log_error"):
			patch_attach_print()
			invoice = frappe.attach_print("Sales Invoice", "ACC-SINV-TEST-1")

		self.assertEqual(invoice["fcontent"], b"%PDF-plain")

	def test_skips_sites_without_app(self):
		from eu_einvoice.european_e_invoice.einvoice import patch_attach_print

		stack, attach_xml = self._patched(return_value=b"%PDF-zugferd")
		with stack, patch("frappe.get_installed_apps", return_value=["frappe", "erpnext"]):
			patch_attach_print()
			invoice = frappe.attach_print("Sales Invoice", "ACC-SINV-TEST-1")

		attach_xml.assert_not_called()
		self.assertEqual(invoice["fcontent"], b"%PDF-plain")

	def test_steps_aside_with_after_print_hook(self):
		from eu_einvoice.european_e_invoice.einvoice import patch_attach_print

		stack, _ = self._patched(after_print_hook=True)
		with stack:
			patch_attach_print()
			self.assertIs(frappe.attach_print, _fake_attach_print)


class TestContactPoint(IntegrationTestCase):
	"""BT-41/BT-56 holds a person or a department, never both (CII-SR-465/466, an error in XRechnung)."""

	def _contact(self, **party):
		from drafthorse.models.document import Document

		from eu_einvoice.european_e_invoice.einvoice import EInvoiceGenerator, TradeParty

		generator = object.__new__(EInvoiceGenerator)
		generator.profile = EInvoiceProfile.XRECHNUNG
		doc = Document()
		generator._write_party(doc.trade.agreement.seller, TradeParty(name="Test GmbH", **party))
		return doc.trade.agreement.seller.contact

	def test_person_wins_over_department(self):
		contact = self._contact(person_name="Erika Muster", department="Buchhaltung")
		self.assertEqual(contact.person_name._text, "Erika Muster")
		self.assertFalse(contact.department_name._text)

	def test_department_without_person(self):
		contact = self._contact(department="Buchhaltung")
		self.assertEqual(contact.department_name._text, "Buchhaltung")
