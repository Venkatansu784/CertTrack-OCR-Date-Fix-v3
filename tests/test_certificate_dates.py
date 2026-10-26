"""Regression checks for date parsing on the provided AWS certificate OCR example.
Run from the project folder with: .\\.venv\\Scripts\\python.exe -m unittest discover -s tests
"""
import unittest

from app import extract_certificate_fields


class CertificateDateExtractionTests(unittest.TestCase):
    def test_issue_and_expiry_are_associated_with_their_labels(self):
        text = """Solutions Architect - Associate Level
Arun subramanian
Has successfully completed the AWS Certification requirements and is recognized
as an AWS Certified Solutions Architect - Associate Level
Certificate AWS-ASA-32001
Director, Training and Certification
February 24, 2017
Certification Issue Date
February 24, 2019
Expiration Date"""
        fields = extract_certificate_fields(text)
        self.assertEqual(fields["issue_date"], "2017-02-24")
        self.assertEqual(fields["expiry_date"], "2019-02-24")
        self.assertEqual(fields["certificate_number"], "AWS-ASA-32001")
        self.assertFalse(fields["_allow_expiry_inference"])

    def test_single_unlabelled_date_is_not_mistaken_for_issue_date(self):
        fields = extract_certificate_fields("February 24, 2019")
        self.assertEqual(fields["issue_date"], "")
        self.assertEqual(fields["expiry_date"], "")
        self.assertFalse(fields["_allow_expiry_inference"])

    def test_month_year_dates_from_amazon_certificate(self):
        fields = extract_certificate_fields("""Gopal Piltai
Certification Date
December-2018
Valid till
December-2019""")
        self.assertEqual(fields["issue_date"], "2018-12-01")
        self.assertEqual(fields["expiry_date"], "2019-12-31")

    def test_ordinal_day_month_year_dates(self):
        fields = extract_certificate_fields("""21st May, 2021
Certification Date
20th May, 2024
Valid till""")
        self.assertEqual(fields["issue_date"], "2021-05-21")
        self.assertEqual(fields["expiry_date"], "2024-05-20")


if __name__ == "__main__":
    unittest.main()


def test_certification_date_label():
    from app import extract_certificate_fields
    fields = extract_certificate_fields("21st May, 2021\nCertification Date\n20th May, 2024\nValid till")
    assert str(fields.get("issue_date", "")).startswith("2021-05-21") or fields.get("issue_date") == "21-05-2021"
    assert str(fields.get("expiry_date", "")).startswith("2024-05-20") or fields.get("expiry_date") == "20-05-2024"
