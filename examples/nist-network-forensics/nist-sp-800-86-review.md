# NIST SP 800-86 review checklist

Use `nist-sp-800-86-forensics-incident-response.pdf` with `nist-sp-800-86-questions.json`. Page numbers below refer to PDF pages, which is how Document RAG cites sources. This is a public guidance document, not an incident report for a named company.

| Question ID | Expected answer | PDF page |
| --- | --- | ---: |
| `forensic-process` | Collection, examination, analysis, and reporting. | 14 |
| `data-source-categories` | Data files, operating systems, network traffic, and applications. | 14 |
| `centralized-logging` | Copies of logs on secure central servers help prevent unauthorized tampering and anti-forensic efforts. | 27 |
| `chain-of-custody` | Record who had physical custody, what they did with the evidence, and when. The same passage also calls for secure storage and integrity checks. | 28 |
| `slack-space` | Unused space in a file allocation unit reserved for a file; it can retain residual data, including parts of deleted files. | 37 |
| `volatile-os-data` | Weigh the risk and effort of collecting it against the chance of recovering important information. | 60 |
| `tcp-ip-layers` | Application, Transport, Internet Protocol, and Hardware (Data Link). | 61 |
| `sem-vs-nfat` | SEM correlates events across existing data sources; NFAT focuses on collecting, examining, and analyzing network traffic. | 68 |
| `original-logs` | Normalization can introduce errors or lose data, so originals allow accuracy checks. | 68 |
| `specific-incident` | Not found. The guide is general guidance and does not describe a NIST investigation of a named compromised server. | — |

Accept equivalent wording when the answer is supported by the cited page. The last question checks that the app does not invent incident-specific facts.
