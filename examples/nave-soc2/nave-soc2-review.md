# Nave SOC 2 UI test

Use the [Nave SOC 2 Type 2 report](https://getnave.com/assets2/docs/Nave-SOC2-Type-2-Report.pdf) as the **Source document** and [`nave-soc2-questions.json`](nave-soc2-questions.json) as **Questions**. The linked PDF returned HTTP 404 during verification on October 13, 2026. Use a local copy and keep it outside Git; do not substitute the Type 1 report without creating new references.

The table below is the existing reference checklist, **not live-model accuracy evidence**. Its page/phrase anchors must be checked against the actual local report. To collect cold/cached latency, usage, and live answers, use the Nave command under **Real PDF latency, token usage, and answer review** in the root README. Mark every generated answer Correct or Incorrect only after reviewing its source support.

This is an 84-page report. The page numbers below refer to PDF pages as shown by the app, which start at 1. Answers may differ in wording, but they should match the facts and cite a relevant page. The last two questions are deliberate missing-data checks.

| Question ID | Expected result | Useful PDF page |
| --- | --- | ---: |
| `audit-period` | August 1 through October 31, 2024 | 1 |
| `scope` | Security | 9 |
| `cloud-provider` | Google Cloud Platform (GCP) | 16 |
| `encryption-at-rest` | AES-256 encrypted disks for data at rest in GCP Storage | 16 |
| `transport-encryption` | TLS 1.2 or above | 16 |
| `security-incidents` | No security incidents during the audit period | 17 |
| `backup` | A fully automated GCP backup schedule for the database | 21 |
| `access-removal` | Promptly, within 24 hours after the IT Manager is notified | 22 |
| `data-classification` | Confidential, Restricted, and Public | 22 |
| `penetration-tests` | At least annually, using a third party | 33 |
| `mfa` | Yes, MFA is required for system authentication | 55 |
| `incident-plan-testing` | At least annually, including simulated events | 78 |
| `subscription-price` | `Data-Not-Found` | - |
| `backup-address` | `Data-Not-Found` | - |

For a first live run, you can temporarily make a copy of the questions JSON with only the first four questions. That keeps the number of paid model calls small while confirming PDF upload, retrieval, page citations, and answer rendering.
