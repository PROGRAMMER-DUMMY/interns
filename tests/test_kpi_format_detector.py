"""Tests for the workspace-agnostic KPI file format detector."""
import unittest

from core.onboarding.kpi.kpi_format_detector import detect_kpi_format


def _rows(columns, *records):
    return [dict(zip(columns, rec)) for rec in records]


class HeaderDrivenMappingTests(unittest.TestCase):
    def test_four_column_file_maps_core_roles_high_confidence(self):
        cols = ["Key Business Question", "Metric", "Cuts / grain hints", "Description"]
        rows = _rows(
            cols,
            ("How many orders shipped?", "count(OrderId)", "Region, Channel", "Volume"),
            ("What is total revenue?", "sum(Amount)", "Region", "Top line"),
        )
        det = detect_kpi_format(cols, rows, source="orders.xlsx")
        self.assertEqual(det.role_header("business_question"), "Key Business Question")
        self.assertEqual(det.role_header("metric"), "Metric")
        self.assertEqual(det.role_header("cuts"), "Cuts / grain hints")
        self.assertEqual(det.role_header("description"), "Description")
        self.assertEqual(det.overall_label, "HIGH")
        self.assertEqual(det.missing_required_roles, [])

    def test_wide_file_maps_known_roles_and_lists_extras_as_unmapped(self):
        cols = ["KPI", "Formula", "Dimensions", "Filters", "Target", "Owner", "JIRA"]
        rows = _rows(
            cols,
            ("Active users", "count(distinct UserId)", "Plan, Country", "status = active", "1000", "Ana", "K-12"),
        )
        det = detect_kpi_format(cols, rows)
        self.assertEqual(det.role_header("business_question"), "KPI")
        self.assertEqual(det.role_header("metric"), "Formula")
        self.assertEqual(det.role_header("cuts"), "Dimensions")
        self.assertEqual(det.role_header("filters"), "Filters")
        self.assertEqual(det.role_header("target"), "Target")
        # Columns with no KPI role are surfaced, not silently dropped.
        self.assertIn("Owner", det.unmapped_headers)
        self.assertIn("JIRA", det.unmapped_headers)

    def test_one_role_per_column_no_double_assignment(self):
        # Two cuts-like headers: only the best-scoring one wins the role.
        cols = ["Question", "Metric", "Dimensions", "Breakdown"]
        rows = _rows(cols, ("How many?", "count(*)", "A, B", "C, D"))
        det = detect_kpi_format(cols, rows)
        cuts_cols = [c.header for c in det.columns if c.role == "cuts"]
        self.assertEqual(len(cuts_cols), 1)


class ContentSignatureFallbackTests(unittest.TestCase):
    def test_generic_headers_resolved_by_values(self):
        # Headers carry no role hint (Col1..Col3); the VALUES must decide.
        cols = ["Col1", "Col2", "Col3"]
        rows = _rows(
            cols,
            ("How many encounters are recorded?", "count(Id)", "Department, Gender"),
            ("What is the readmission rate?", "ratio(readmits, admits)", "Department"),
        )
        det = detect_kpi_format(cols, rows)
        self.assertEqual(det.role_header("business_question"), "Col1")
        self.assertEqual(det.role_header("metric"), "Col2")
        self.assertEqual(det.role_header("cuts"), "Col3")
        # Header gave no signal, so confidence is content-driven (not HIGH) and
        # therefore flagged for confirmation.
        metric_col = next(c for c in det.columns if c.role == "metric")
        self.assertLess(metric_col.confidence, 0.85)
        self.assertTrue(metric_col.to_dict()["needs_user_confirmation"])

    def test_missing_metric_role_lowers_confidence_and_is_reported(self):
        cols = ["Key Business Question", "Cuts"]
        rows = _rows(cols, ("How many orders?", "Region"))
        det = detect_kpi_format(cols, rows)
        self.assertIn("metric", det.missing_required_roles)
        self.assertNotEqual(det.overall_label, "HIGH")
        self.assertTrue(det.to_dict()["needs_user_confirmation"])


class ContentOverridesMisleadingHeaderTests(unittest.TestCase):
    def test_decisive_metric_content_beats_a_misleading_description_header(self):
        # "Detail" header synonym-matches `description`, but its values are all
        # aggregate formulas -> it is really the metric. Decisive content must win
        # the assignment (so metric is NOT reported missing), while the conflict
        # keeps it below HIGH so it is still flagged for confirmation.
        cols = ["Item", "Detail", "Slice"]
        rows = _rows(
            cols,
            ("What is share of lives by department?",
             "percentage of count(distinct PatientID) / count(distinct PatientID) for department",
             "Department"),
            ("How many encounters?", "count(Id)", "Department, VisitType"),
        )
        det = detect_kpi_format(cols, rows)
        self.assertEqual(det.role_header("metric"), "Detail")
        self.assertNotIn("metric", det.missing_required_roles)
        metric_col = next(c for c in det.columns if c.role == "metric")
        self.assertLess(metric_col.confidence, 0.85)  # still flagged for confirmation
        self.assertTrue(metric_col.to_dict()["needs_user_confirmation"])


class MergedCellNestingTests(unittest.TestCase):
    def test_merged_span_in_key_column_flags_nesting(self):
        cols = ["Key Business Question", "Metric", "Cuts"]
        rows = _rows(
            cols,
            ("Share of lives", "count(distinct PatientId)", "Department"),
            ("Share of lives", "count(distinct PatientId)", "Gender"),
        )
        # Key column (index 0) merged across data rows 0-1 = a parent bracketing
        # sub-KPIs. (col_index, row_start, row_end)
        det = detect_kpi_format(cols, rows, merged_spans=[(0, 0, 1)])
        self.assertTrue(det.nesting_suspected)
        self.assertTrue(any("merged-cell" in s for s in det.nesting_signals))

    def test_merged_span_in_non_key_column_does_not_flag_nesting(self):
        cols = ["Key Business Question", "Metric", "Cuts"]
        rows = _rows(cols, ("How many orders?", "count(*)", "Region"))
        # A merge in a non-key column (e.g. a header band) is not KPI nesting.
        det = detect_kpi_format(cols, rows, merged_spans=[(2, 0, 1)])
        self.assertFalse(det.nesting_suspected)


class ReadBackTests(unittest.TestCase):
    def test_read_back_maps_row_to_roles_in_canonical_order(self):
        cols = ["Key Business Question", "Metric", "Cuts"]
        rows = _rows(cols, ("What is total revenue by region?", "sum(Amount)", "Region"))
        det = detect_kpi_format(cols, rows)
        read = det.read_back(rows[0])
        self.assertEqual(read["business_question"], "What is total revenue by region?")
        self.assertEqual(read["metric"], "sum(Amount)")
        self.assertEqual(read["cuts"], "Region")
        # Canonical ordering: business_question precedes metric precedes cuts.
        self.assertEqual(list(read.keys()), ["business_question", "metric", "cuts"])


class NestingDetectionTests(unittest.TestCase):
    def test_blank_key_rows_under_parent_flag_nesting(self):
        cols = ["Key Business Question", "Metric", "Cuts"]
        rows = _rows(
            cols,
            ("Share of lives", "count(distinct PatientId)", "Department"),
            ("", "", "Gender"),
            ("", "", "Age band"),
        )
        det = detect_kpi_format(cols, rows)
        self.assertTrue(det.nesting_suspected)
        self.assertTrue(det.nesting_signals)

    def test_hierarchical_numbering_flags_nesting(self):
        cols = ["KPI", "Metric"]
        rows = _rows(cols, ("1 Revenue", "sum(Amount)"), ("1.1 Revenue by region", "sum(Amount)"))
        det = detect_kpi_format(cols, rows)
        self.assertTrue(det.nesting_suspected)


class GenericityTests(unittest.TestCase):
    def test_no_domain_vocabulary_required_e_commerce_and_support(self):
        # Same detector, two unrelated domains, identical behaviour.
        cols = ["Question", "Metric", "Dimensions"]
        ecom = _rows(cols, ("What is cart abandonment?", "ratio(abandoned, started)", "Device"))
        support = _rows(cols, ("How many tickets resolved?", "count(TicketId)", "Queue, Priority"))
        for rows in (ecom, support):
            det = detect_kpi_format(cols, rows)
            self.assertEqual(det.role_header("metric"), "Metric")
            self.assertEqual(det.role_header("cuts"), "Dimensions")
            self.assertEqual(det.role_header("business_question"), "Question")


if __name__ == "__main__":
    unittest.main()
