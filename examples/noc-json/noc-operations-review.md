# NOC JSON test guide

Upload [`noc-operations-source.json`](noc-operations-source.json) as **Source document** and [`noc-operations-questions.json`](noc-operations-questions.json) as **Questions**. Both are fictional and need no download. This pair tests JSON-path citations across multiple incident records and runbooks.

| Question ID | Expected answer | Supporting JSON path |
| --- | --- | --- |
| `route-incident` | Bad gateway route; rollback at 09:41 UTC | `$.incidents[0]` |
| `edge-impact` | About 28 percent | `$.incidents[0]` |
| `eu-webhooks` | NOC-2026-021; no events lost | `$.incidents[1]` |
| `metrics` | No; dashboards lagged, customer traffic unaffected | `$.incidents[2]` |
| `auth` | Expired internal mutual-TLS certificate | `$.incidents[3]` |
| `dns` | Flushed resolver cache and lowered TTL | `$.incidents[4]` |
| `replica` | Up to 18 minutes | `$.incidents[5]` |
| `sev1-ack` | Within five minutes | `$.runbooks.alert_triage` |
| `escalation` | After 15 minutes unresolved | `$.runbooks.alert_triage` |
| `packet-loss` | More than two percent for five minutes | `$.runbooks.monitoring_thresholds` |
| `status-cadence` | At least every 30 minutes | `$.runbooks.communications` |
| `maintenance` | Sunday, 02:00-04:00 UTC | `$.runbooks.planned_maintenance` |
| `maintenance-notice` | At least 48 hours | `$.runbooks.planned_maintenance` |
| `emergency-approval` | Incident Commander | `$.runbooks.emergency_changes` |
| `unknown-credit` | `Data-Not-Found` | - |
| `unknown-address` | `Data-Not-Found` | - |

Compare answer meaning and citations rather than exact wording. The last two questions deliberately have no answer in the source.
