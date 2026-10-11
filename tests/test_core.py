import unittest

from bulksms import csvin, template
from bulksms.phone import to_e164
from bulksms.server import is_local


class PhoneTest(unittest.TestCase):
    def test_every_way_of_typing_the_same_number(self):
        for raw in ("070-123 45 67", "0701234567", "+46701234567", "+46 70 123 45 67",
                    "0046701234567", "+46 (0)70 123 45 67", "+46 070 123 45 67", "070.123.45.67"):
            self.assertEqual(to_e164(raw), "+46701234567", raw)

    def test_what_is_not_a_number(self):
        for raw in ("", "701234567", "abc", "070-12", "+46 70 12x 45 67", "070123456789012345"):
            self.assertIsNone(to_e164(raw), raw)

    def test_foreign_numbers_and_other_home_countries(self):
        self.assertEqual(to_e164("+47 412 34 567"), "+4741234567")
        self.assertEqual(to_e164("0412 345 678", "61"), "+61412345678")


class CsvTest(unittest.TestCase):
    def test_semicolons_with_a_header(self):
        rows = csvin.parse("Namn;Telefon;Ort\nAnna Andersson;070-123 45 67;Lund\nBo;0735556677;Ystad")
        self.assertEqual([r["name"] for r in rows], ["Anna Andersson", "Bo"])
        self.assertEqual(rows[0]["phone_raw"], "070-123 45 67")
        self.assertEqual(rows[0]["fields"]["Ort"], "Lund")

    def test_no_header_and_phone_first(self):
        rows = csvin.parse("0701234567,Anna\n0735556677,Bo")
        self.assertEqual([(r["name"], r["phone_raw"]) for r in rows], [("Anna", "0701234567"), ("Bo", "0735556677")])

    def test_a_mistyped_first_row_is_not_taken_for_a_header(self):
        rows = csvin.parse("Anna,070-12345\nBo,0735556677")
        self.assertEqual(len(rows), 2)

    def test_tabs_from_a_spreadsheet(self):
        rows = csvin.parse("Anna\t070-123 45 67\nBo\t073-555 66 77")
        self.assertEqual(rows[1], {"name": "Bo", "phone_raw": "073-555 66 77", "fields": {}})

    def test_free_text_lines(self):
        rows = csvin.parse("Anna Andersson 070-123 45 67\n+46735556677 Bo\n0701112233")
        self.assertEqual([r["name"] for r in rows], ["Anna Andersson", "Bo", ""])
        self.assertEqual(to_e164(rows[1]["phone_raw"]), "+46735556677")

    def test_a_single_column_of_numbers(self):
        self.assertEqual(len(csvin.parse("0701234567\n0735556677\n")), 2)


class TemplateTest(unittest.TestCase):
    def test_name_and_columns_any_case(self):
        vals = template.values("Anna Andersson", {"Ort": "Lund"}, first_name_only=True)
        self.assertEqual(template.render("Hej [Name] i [ORT]! [x]", vals), "Hej Anna i Lund! [x]")
        self.assertEqual(template.unknown_keys("Hej [name] [ort] [x]", ["ort"]), ["x"])

    def test_empty_values_are_reported(self):
        self.assertEqual(template.empty_keys("Hej [name]", template.values("", {})), ["name"])
        self.assertEqual(template.empty_keys("Hej", template.values("", {})), [])

    def test_segments(self):
        self.assertEqual(template.segments("å" * 160), (160, 1, "GSM-7"))
        self.assertEqual(template.segments("a" * 161), (161, 2, "GSM-7"))
        self.assertEqual(template.segments("[" * 80)[0], 160)
        self.assertEqual(template.segments("😀" * 36), (72, 2, "Unicode"))


class LanTest(unittest.TestCase):
    def test_only_private_addresses(self):
        for ok in ("127.0.0.1", "192.168.1.20", "10.0.0.5", "172.20.1.1", "::1", "::ffff:192.168.1.9", "fe80::1%wlan0"):
            self.assertTrue(is_local(ok), ok)
        for no in ("8.8.8.8", "100.64.3.2", "172.32.0.1", "2001:db8::1", "::ffff:8.8.8.8", "junk"):
            self.assertFalse(is_local(no), no)


if __name__ == "__main__":
    unittest.main()
