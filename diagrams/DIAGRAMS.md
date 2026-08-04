# UML Diagrams — Scheduling Agent Core

This package walks through the design at three levels of altitude, each one step closer to the code: the **activity diagram** shows the broad business process a booking request goes through, the **sequence diagram** shows the same journey as concrete component-to-component behaviour, and the **class diagram** shows the actual objects that behaviour is implemented with. Read in this order — each diagram answers "how does what I just saw actually happen?"

Sources (`.mmd`, Mermaid) live alongside this file and render natively in any Mermaid-aware viewer, including directly on GitHub. `UML_Diagrams.docx` in this same folder is an exported, standalone copy of this walkthrough with rendered images, for sharing outside the repo (e.g. printing, or reading before an interview without cloning anything).

This package expands on — and is referenced from — [`../PLAN.md`](../PLAN.md) and [`../ARCHITECTURE.md`](../ARCHITECTURE.md). It does not replace the component diagram or data contracts in `ARCHITECTURE.md`; it goes one level deeper into *behaviour* and *object structure*.

---

## 1. The broad picture — Activity Diagram

Source: [`activity-diagram.mmd`](activity-diagram.mmd)

```mermaid
flowchart TD
    subgraph Customer
        A1(["Send free-text booking request"])
        A9(["Receive confirmation or pending notice"])
    end
    subgraph System["Scheduling Agent System"]
        A2{"Intake mode: LLM or offline?"}
        A3["LLM parses free text"]
        A4["Offline deterministic parse"]
        A5["Structured Booking Request"]
        A6["Load Sector Manifest + Skill Pack rules"]
        A7{"Rules satisfied and no conflict?"}
        A8a["Decision: CONFIRMED"]
        A8b["Decision: PENDING_APPROVAL + reason"]
        A10["Append Audit Record"]
    end
    subgraph Staff
        A11(["Review PENDING_APPROVAL items"])
    end

    A1 --> A2
    A2 -->|LLM| A3
    A2 -->|offline| A4
    A3 --> A5
    A4 --> A5
    A5 --> A6
    A6 --> A7
    A7 -->|yes| A8a
    A7 -->|no| A8b
    A8a --> A10
    A8b --> A10
    A10 --> A9
    A8b -.notifies.-> A11
```

**What this shows:** the end-to-end business process, independent of which classes or functions implement it. Three swimlanes — Customer, the Scheduling Agent System, and Staff — make clear who does what. Deliberately, this diagram does not distinguish clinic from restaurant: at this altitude, **both sectors are the same process**. That sameness is the whole point of the design (FR1 / R1) — sector differences only enter at "Load Sector Manifest + Skill Pack rules," as *data*, not as a fork in the process itself.

**Why it comes first:** before looking at which components talk to which, it's worth confirming the shape of the process a non-engineer (a stakeholder, an interviewer skimming the submission) would recognise: send a request, get parsed, get checked against rules, get confirmed or held for review, get logged either way.

---

## 2. One level deeper — Sequence Diagram (behaviour)

Source: [`sequence-diagram.mmd`](sequence-diagram.mmd)

```mermaid
sequenceDiagram
    actor Cust as Customer
    participant Intake as Intake Service
    participant Core as Scheduling Agent Core
    participant Manifest as Sector Manifest
    participant SP as Skill Pack
    participant Gate as Approval Gate
    participant Store as Booking Store
    participant Audit as Audit Trail

    Cust->>Intake: free text ("table for 4 tomorrow 8pm")
    alt LLM mode
        Intake->>Intake: call LLM, extract structured fields
    else offline mode
        Intake->>Intake: deterministic rule/regex parse
    end
    Intake-->>Core: BookingRequest{sector, fields}
    Core->>Manifest: load(sector)
    Manifest-->>Core: enabled, skill_pack ref, approval_required_for
    Core->>SP: get rules()
    SP-->>Core: required_fields, working_hours, slot_rules, template
    Core->>Store: check_conflict(request)
    Store-->>Core: conflict: true/false
    Core->>Gate: evaluate(request, rules, conflict)
    alt within rules, no conflict
        Gate-->>Core: CONFIRMED
        Core->>Store: persist(booking)
    else violates a rule or conflicts
        Gate-->>Core: PENDING_APPROVAL(reason)
    end
    Core->>Audit: append(input, skill_pack, intent, rules_evaluated, decision, status)
    Core-->>Cust: confirmation message or PENDING_APPROVAL + reason
```

**What this shows:** the same journey as the activity diagram, but now as concrete messages between named components — the ones that appear in the class diagram below. Every box in the activity diagram's "System" swimlane corresponds to one or more of these calls:

| Activity diagram step | Sequence diagram participant(s) |
|---|---|
| "LLM parses" / "Offline deterministic parse" | `Intake Service` |
| "Load Sector Manifest + Skill Pack rules" | `Sector Manifest`, `Skill Pack` |
| "Rules satisfied and no conflict?" | `Booking Store` (conflict check) + `Approval Gate` (rule evaluation) |
| "Append Audit Record" | `Audit Trail` |

**What's new at this level:** the `Scheduling Agent Core` is now visibly the *only* participant that talks to every other component — `Intake Service`, `Sector Manifest`, `Skill Pack`, `Approval Gate`, `Booking Store`, and `Audit Trail` never talk to each other directly. That star topology, not just "no sector names in the core," is what makes FR1 hold structurally rather than by convention.

---

## 3. Into the architecture — Class Diagram

Source: [`class-diagram.mmd`](class-diagram.mmd)

```mermaid
classDiagram
    class BookingRequest {
        +str sector
        +dict fields
        +str raw_text
    }
    class Decision {
        +str status
        +str reason
    }
    class AuditRecord {
        +str input
        +str skill_pack
        +dict intent
        +list rules_evaluated
        +str decision
        +str approval_status
        +datetime timestamp
    }
    class SectorManifest {
        +str sector
        +bool enabled
        +str skill_pack
        +list approval_required_for
        +load(sector) SectorManifest
    }
    class SkillPack {
        <<abstract>>
        +list required_fields
        +dict working_hours
        +validate(request) list
        +slot_rules(request) SlotInfo
        +confirmation_template() str
    }
    class ClinicSkillPack {
        fixed slot length per practitioner
    }
    class RestaurantSkillPack {
        capacity and party-size rules
    }
    class IntakeService {
        <<interface>>
        +parse(text) BookingRequest
    }
    class LLMIntake {
        +parse(text) BookingRequest
    }
    class OfflineIntake {
        +parse(text) BookingRequest
    }
    class ApprovalGate {
        +evaluate(request, rules, conflict) Decision
    }
    class BookingStore {
        +check_conflict(request) bool
        +persist(request)
    }
    class AuditTrail {
        +append(record) void
    }
    class SchedulingAgentCore {
        -IntakeService intake
        -ApprovalGate gate
        -BookingStore store
        -AuditTrail audit
        +handle(text, sector) Decision
    }

    SkillPack <|-- ClinicSkillPack
    SkillPack <|-- RestaurantSkillPack
    IntakeService <|.. LLMIntake
    IntakeService <|.. OfflineIntake
    SchedulingAgentCore --> IntakeService
    SchedulingAgentCore --> ApprovalGate
    SchedulingAgentCore --> BookingStore
    SchedulingAgentCore --> AuditTrail
    SchedulingAgentCore --> SectorManifest
    SchedulingAgentCore --> SkillPack
    SchedulingAgentCore ..> BookingRequest
    SchedulingAgentCore ..> Decision
    ApprovalGate ..> Decision
    AuditTrail --> AuditRecord
```

**What this shows:** the two abstraction boundaries the whole design rests on:

- **`SkillPack`** is abstract; `SchedulingAgentCore` depends only on that abstraction, never on `ClinicSkillPack` or `RestaurantSkillPack` by name. Adding a third sector means writing a third subclass — zero changes to `SchedulingAgentCore`. This is FR1 and the extension-point map in `ARCHITECTURE.md`, expressed as a class relationship rather than a rule.
- **`IntakeService`** is an interface with two interchangeable implementations, `LLMIntake` and `OfflineIntake`. `SchedulingAgentCore` is written against the interface, so the deterministic-offline requirement (FR4) is satisfied by *swapping an implementation*, not by branching inside the core on "are we in test mode."

**Why it comes last:** this is the diagram the implementation is written against line-for-line, and the one the live interview's extension exercise will probe directly — "show me the third subclass, show me it required no core changes." The activity and sequence diagrams above are what make that class structure legible as a business process and a runtime behaviour, rather than an abstraction for its own sake.
