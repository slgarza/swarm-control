# GUI design

Swarm Control is a local operations tool. Its interface should make the selected
repository, run, next action and recorded outcome easy to find. The worker cohort,
task review and budget records are the product's visual identity.

## Presentation rules

- Keep the dark surfaces and mint selection/action accent. Status colors always
  accompany readable labels; original execution, provisional recovery and unknown
  billing remain distinct.
- Use the system UI font for controls and prose. Reserve the monospace face for
  identifiers, source references and commands. Define colors and fonts through
  the shared root tokens in `dashboard_static/styles.css`.
- Operational labels and help text should remain readable at normal zoom. Use
  12px or larger for ordinary interface text; compact worker identifiers may use
  11px. Check actual contrast on the surface where a label appears.
- Name sections after the work: repository selection, run history, worker cohort,
  tasks, budget and evidence. Decorative category labels and promotional headlines
  add little to an operations screen.
- Use a single boundary for each region. Stat rows, form sections and step lists
  use alignment and separators instead of repeated cards inside cards.
- Keep visible keyboard focus, native controls and disclosures, reduced-motion
  behavior and usable mobile hit targets. Tables may scroll within their region;
  the page itself should not spill horizontally.

## Changing the GUI

The static interface is in `swarm_control/dashboard_static/`. Preserve IDs,
selectors, field names, event handlers, native disabled states and API contracts
when changing its appearance. Keep authorization, credential storage, budgets and
provider actions under the existing controller gates. Consent and verification
copy must remain explicit.

Review empty, loading, error and populated views, plus all Setup steps. Use
synthetic fixtures for screenshots and preserve the distinction between test
data and actual execution evidence. Check mobile and desktop layouts, keyboard
navigation, long repository/task names and the recorded run state.

## Design references

[Hallmark](https://github.com/Nutlope/hallmark) 2.0.0 was reviewed at commit
`04977e15f3dfd6ee4b74c887dac3eb52665d6c92` and used for an app-screen polish and
browser review. It is an authoring aid, not an application dependency. This pass
uses Swarm Control's existing visual system rather than a landing-page theme.

[Impeccable](https://github.com/pbakaus/impeccable) provides additional audit,
critique and polish workflows. Anthropic's
[frontend-design](https://github.com/anthropics/skills/tree/main/skills/frontend-design)
offers broader guidance on intentional typography, composition and copy. These
are review references; the controller's contracts and the operator's intent
remain authoritative.
