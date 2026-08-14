"""The design you approve sits in the gate's evidence packet (task b7b71225).

Owner report 2026-08-14: at plan_gate the gate banner said "packet ready
(story/plan, diagram) - inspect the evidence below", but the EVIDENCE section
rendered only the implementation DecisionPacket (Diff none, Commits none,
Oracle receipt none, Visual evidence none) while the design actually under
approval sat below the fold inside PlanView's design tab. "Its confusing
where to do [the review]".

The PRISM SPA has NO JS test runner (repo convention), so the UI ACs are
pinned by asserting the ACTUAL TSX source. Per the source-reading lessons the
assertions match the RENDERED TAG (`<DesignPacket`) and guard EXPRESSIONS,
never bare names a comment could satisfy.
"""

from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "prism_service" / "web" / "src"
_PAGE = _WEB / "pages" / "TaskDetailPage.tsx"
_PACKET = _WEB / "components" / "plan" / "DesignPacket.tsx"


def test_gate_evidence_carries_the_design_packet():
    src = _PAGE.read_text(encoding="utf-8")

    # The gate panel's evidence section mounts the design packet when the
    # gate awaits a design approval - between the section header and the
    # machine-check table the banner points the reviewer at.
    evidence_at = src.index(">evidence</div>")
    table_at = src.index('<table className="w-full text-[12.5px]">', evidence_at)
    section = src[evidence_at:table_at]

    mount_at = section.index("<DesignPacket")
    guard_at = section.index("isAwaitingDesignApproval && (")
    assert guard_at < mount_at, (
        "the DesignPacket mount must be guarded by isAwaitingDesignApproval - "
        "it is the evidence only while a design approval is what the gate waits on"
    )

    # No forked approve: the packet in the gate panel hides its own footer;
    # the panel's decision controls are the single affordance (task 76df7520).
    assert "hideApproval" in section[mount_at:]

    # The implementation DecisionPacket yields at the design-approval state
    # (all-"none" rows posing as the evidence was the confusion) but still
    # renders at every other gate - deleting it everywhere would hide real
    # implementation evidence from red/green gates.
    dp_at = section.index("<DecisionPacket")
    dp_guard = section.rindex("!isAwaitingDesignApproval && (", 0, dp_at)
    assert dp_guard < dp_at, (
        "DecisionPacket must be suppressed only while the design approval is "
        "pending, never unconditionally"
    )


def test_design_packet_footer_hides_behind_the_prop():
    src = _PACKET.read_text(encoding="utf-8")

    # The prop exists on the component contract.
    assert "hideApproval?: boolean" in src

    # The approval footer (the block that renders the "Approve design"
    # button) is inside the !hideApproval guard - matched as the guard
    # EXPRESSION preceding the rendered button text.
    guard_at = src.index("{!hideApproval && (")
    button_at = src.index("Approve design")
    assert guard_at < button_at, (
        "the Approve design affordance must not render when the gate panel "
        "mounts the packet as evidence - two approve buttons fork the single "
        "sign-off"
    )
