"""Payment schedule cardinality, independent of any extension app."""

from unittest import TestCase
from unittest.mock import patch

import frappe
from drafthorse.models.document import Document

from eu_einvoice.european_e_invoice.custom.sales_invoice import SalesInvoiceGenerator
from eu_einvoice.utils import EInvoiceProfile


class TestPaymentTerms(TestCase):
	def generator(self, profile):
		invoice = frappe._dict(
			posting_date="2026-09-01", due_date="2026-10-01", currency="EUR", outstanding_amount=1190,
			payment_schedule=[
				frappe._dict(payment_amount=595, due_date="2026-09-15", description="First # instalment",
					discount=2, discount_type="Percentage", discount_date="2026-09-10"),
				frappe._dict(payment_amount=595, due_date="2026-10-01", description="Second instalment"),
			],
		)
		generator = SalesInvoiceGenerator(invoice, profile)
		generator.doc = Document()
		return generator

	def test_profile_cardinality_preserves_instalment_information(self):
		with patch("eu_einvoice.european_e_invoice.einvoice._", side_effect=lambda value: value):
			for profile in EInvoiceProfile:
				with self.subTest(profile=profile):
					generator = self.generator(profile)
					generator._add_payment_terms()
					terms = generator.doc.trade.settlement.terms.children
					self.assertEqual(len(terms), 2 if profile == EInvoiceProfile.EXTENDED else 1)
					if profile != EInvoiceProfile.EXTENDED:
						self.assertFalse(terms[0].partial_amount.children)
						self.assertIn("2026-09-15", terms[0].description._text)
						self.assertIn("2026-10-01", terms[0].description._text)
						self.assertIn("595.00 EUR", terms[0].description._text)
						if profile == EInvoiceProfile.XRECHNUNG:
							self.assertIn("First // instalment", terms[0].description._text)
							self.assertIn("#SKONTO#TAGE=9#PROZENT=2.00#BASISBETRAG=595.00#\n", terms[0].description._text)
					else:
						self.assertEqual(len(terms[0].partial_amount.children), 1)
						self.assertEqual(float(terms[0].discount_terms.calculation_percent._value), 2)
