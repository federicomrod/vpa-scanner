"""Report rendering.

Turns a scan result into the Markdown report and JSON record that
(eventually) get emailed each morning. See report.py for the actual
rendering code - delivery (sending them anywhere) is a separate, later
pull request.
"""

from vpa.reporting.report import ReportPaths, render_json, render_markdown, write_report

__all__ = ["ReportPaths", "render_json", "render_markdown", "write_report"]
