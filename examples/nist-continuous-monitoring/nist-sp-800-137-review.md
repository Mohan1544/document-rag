# NIST SP 800-137 review checklist

Use `nist-sp-800-137-continuous-monitoring.pdf` with `nist-sp-800-137-questions.json`. Page numbers below refer to PDF pages, which is how Document RAG cites sources. This is a public guidance document, not a SOC 2 audit report.

| Question ID | Expected answer | PDF page |
| --- | --- | ---: |
| `iscm-definition` | Ongoing awareness of information security, vulnerabilities, and threats to support organizational risk decisions. | 8 |
| `risk-tiers` | Tier 1: organization; Tier 2: mission/business processes; Tier 3: information systems. | 10 |
| `automation-limits` | Humans must implement and maintain tools, interpret findings, and run the processes in which the tools operate. | 20 |
| `monitoring-frequency` | System owners review organizational minimums and assess or monitor particular controls more frequently when the system needs it. | 35 |
| `event-driven-assessment` | Examples include incidents, new threat information, significant system or environment changes, new mission responsibilities, and security impact or risk assessment results. | 36 |
| `tier-3-reporting` | Findings, system-level mitigations, and recommendations. | 39 |
| `automation-domains` | Vulnerability Management, Patch Management, and Incident Management. | 65 |
| `siem-functions` | Analyze and correlate audit records, identify and prioritize significant events, and optionally initiate responses. | 73 |
| `company-audit-period` | Not found. The guide does not report on Northstar Review Cloud or its SOC 2 period. | — |

Accept equivalent wording when the answer is supported by the cited page. The last question checks that the app does not invent a company-specific answer.
