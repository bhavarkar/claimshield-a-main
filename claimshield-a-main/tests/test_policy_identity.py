from claimshield.models import Bill, Clause, Finding, FindingType, PolicyCitation, PolicyDoc
from claimshield.agents.steps import _uncertain_policy_topics
from claimshield.services.finding_evidence import attach_finding_evidence
from claimshield.services.parsing import RawDocument, normalize_policy, policy_document_identity
from claimshield.services.retrieval import LocalRetriever, VertexSearchRetriever


def _finding(citation):
    return Finding(finding_id="F1", type=FindingType.COVERAGE_MISMATCH, title="Coverage review",
                   bill_evidence="Bill amount", policy=citation, explanation="Review the policy wording",
                   confidence="Worth reviewing", impact=0, counterparty="insurer",
                   detected_by="test", method="test")


def _raw(text):
    return RawDocument(pages=["Example Insurer\nExample Plan\n" + text], document_id=None)


def test_policy_hash_is_stable_and_distinguishes_same_filename_documents():
    first, second = b"first policy bytes", b"second policy bytes"
    assert policy_document_identity(first) == policy_document_identity(first)
    assert policy_document_identity(first) != policy_document_identity(second)
    a = normalize_policy(_raw("2.1 Room Rent. Room rent is payable."), "same.pdf", policy_document_identity(first))
    b = normalize_policy(_raw("2.1 Room Rent. Room rent is payable."), "same.pdf", policy_document_identity(second))
    assert a.source == b.source == "same.pdf"
    assert a.document_id != b.document_id
    assert a.clauses[0].document_id == a.document_id


def test_citation_must_match_selected_document_clause_and_version():
    clause = Clause(clause_id="2.1", title="Room Rent", text="Room rent is payable.", page=1,
                    source="same.pdf", document_id="doc-A", version_id="version-A")
    policy = PolicyDoc(source="same.pdf", document_id="doc-A", version_id="version-A", clauses=[clause])
    citation = PolicyCitation(clause_id="2.1", title=clause.title, quote=clause.text, page=1,
                              source="same.pdf", document_id="doc-B", version_id="version-B")
    result = attach_finding_evidence(_finding(citation), Bill(), policy, "bill-key", "policy-key")
    assert result.evidence["policy"]["status"] == "unavailable"
    assert result.evidence["policy"]["document_id"] == "doc-A"


def test_local_retrieval_rejects_weak_matches_and_threshold_is_configurable():
    policy = PolicyDoc(clauses=[Clause(clause_id="1.1", title="Room", text="Room rent limit is Rs 5000 per day.")])
    assert LocalRetriever(min_score=0.01).search(policy, "room rent limit", top_k=1)
    assert LocalRetriever(min_score=1000).search(policy, "room rent limit", top_k=1) == []


def test_missing_and_unknown_clause_citations_are_unavailable():
    policy = PolicyDoc(source="policy.pdf", document_id="doc", version_id="doc", clauses=[
        Clause(clause_id="1.1", title="Room", text="Room rent limit.", source="policy.pdf",
               document_id="doc", version_id="doc")])
    for clause_id in ("1.1", "?"):
        citation = PolicyCitation(clause_id=clause_id, title="Room", quote="different quote", page=1,
                                  source="policy.pdf", document_id="doc", version_id="doc")
        result = attach_finding_evidence(_finding(citation), Bill(), policy, "bill", "policy")
        assert result.evidence["policy"]["status"] == "unavailable"


def test_vertex_excerpt_without_exact_selected_clause_is_not_returned():
    retriever = object.__new__(VertexSearchRetriever)
    policy = PolicyDoc(source="same.pdf", document_id="selected", clauses=[
        Clause(clause_id="2.1", title="Room Rent", text="Room rent is limited to Rs 5000.", source="same.pdf")])

    class Span:
        page_start = 2

    class Chunk:
        page_span = Span()

    assert retriever._to_clause(policy, "2.1 Room Rent. Generic unrelated excerpt.", Chunk()) is None
    matched = retriever._to_clause(policy, "2.1 Room Rent. Room rent is limited to Rs 5000.", Chunk())
    assert matched is not None and matched.clause_id == "2.1"


def test_selected_version_conflict_does_not_verify_evidence():
    clause = Clause(clause_id="3.1", title="Co-pay", text="A co-payment applies.", page=1,
                    source="policy.pdf", document_id="doc", version_id="v2")
    policy = PolicyDoc(source="policy.pdf", document_id="doc", version_id="v2", clauses=[clause])
    citation = PolicyCitation(clause_id="3.1", title=clause.title, quote=clause.text, page=1,
                              source=clause.source, document_id="doc", version_id="v1")
    result = attach_finding_evidence(_finding(citation), Bill(), policy, "bill", "policy")
    assert result.evidence["policy"]["status"] == "unavailable"


def test_uncertain_wording_is_explicitly_marked():
    topics = {"copay": Clause(clause_id="4.1", title="Co-pay",
                              text="A co-payment may be considered at the discretion of the insurer.")}
    assert _uncertain_policy_topics(topics) == ["copay"]
