import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import type { FindingDetail, FindingWithReview, ReviewRequest } from "../../../contracts/api";
import type { InvestigationEvent, Region, Severity } from "../../../contracts/types";
import { api } from "../api/client";
import { DocumentViewer } from "../components/DocumentViewer";
import type { DocumentViewerHandle } from "../components/DocumentViewer";
import {
  SortButton,
  Breadcrumbs,
  Button,
  Confidence,
  EmptyState,
  ErrorState,
  Icon,
  LoadingState,
  ObservationLabel,
  PageHeader,
  StatusBadge,
} from "../components/ui";
import { mockMode } from "../config";
import { fixtureAnalysis } from "../mockData";

type SortKey = "severity" | "confidence" | "type" | "category";
const severityOrder: Record<Severity, number> = { high: 0, medium: 1, low: 2, info: 3 };

export function FindingsPage() {
  const [searchParams] = useSearchParams();
  const [caseFilter, setCaseFilter] = useState(() => searchParams.get("caseId") ?? "all");
  const [category, setCategory] = useState("all");
  const [observation, setObservation] = useState("all");
  const [review, setReview] = useState("all");
  const [severity, setSeverity] = useState("all");
  const [document, setDocument] = useState("all");
  const [sortKey, setSortKey] = useState<SortKey>("severity");
  const [descending, setDescending] = useState(false);
  const [search, setSearch] = useState("");
  const casesQuery = useQuery({ queryKey: ["cases"], queryFn: api.listCases });
  const cases = casesQuery.data ?? [];
  const activeCaseFilter = caseFilter === "all" || cases.some((item) => item.caseId === caseFilter) ? caseFilter : "all";
  const findingsQuery = useQuery({
    queryKey: ["findings", activeCaseFilter],
    queryFn: async () => {
      const selectedCases = activeCaseFilter === "all" ? cases : cases.filter((item) => item.caseId === activeCaseFilter);
      const responses = await Promise.all(selectedCases.map((item) => api.findings(item.caseId)));
      return responses.flatMap((response) => response.findings);
    },
    enabled: cases.length > 0,
  });
  const records = findingsQuery.data ?? [];
  const docs = useMemo(() => Array.from(new Set(records.flatMap((record) =>
    record.finding.evidence.flatMap((evidence) => evidence.regions.map((region) => region.docId)),
  ))), [records]);
  const categories = useMemo(() => Array.from(new Set(records.map(({ finding }) => finding.category))).sort(), [records]);
  const filtered = useMemo(() => {
    const result = records.filter(({ finding, reviewStatus }) => {
      const matchesSearch = `${finding.type} ${finding.findingId} ${finding.category}`.toLowerCase().includes(search.toLowerCase());
      const matchesDocument = document === "all" || finding.evidence.some((evidence) => evidence.regions.some((region) => region.docId === document));
      return matchesSearch &&
        (category === "all" || finding.category === category) &&
        (observation === "all" || finding.observationStatus === observation) &&
        (review === "all" || reviewStatus === review) &&
        (severity === "all" || finding.severity === severity) &&
        matchesDocument;
    });
    result.sort((left, right) => {
      const a = left.finding;
      const b = right.finding;
      let comparison = 0;
      if (sortKey === "severity") comparison = severityOrder[a.severity] - severityOrder[b.severity];
      else if (sortKey === "confidence") comparison = a.confidence - b.confidence;
      else comparison = a[sortKey].localeCompare(b[sortKey]);
      return descending ? -comparison : comparison;
    });
    return result;
  }, [records, category, observation, review, severity, document, search, sortKey, descending]);

  if (casesQuery.isLoading || findingsQuery.isLoading) return <LoadingState label="Loading recorded findings" />;
  if (casesQuery.isError) return <ErrorState error={casesQuery.error} retry={() => void casesQuery.refetch()} />;
  if (findingsQuery.isError) return <ErrorState error={findingsQuery.error} retry={() => void findingsQuery.refetch()} />;

  function setSort(next: SortKey) {
    if (sortKey === next) setDescending((value) => !value);
    else {
      setSortKey(next);
      setDescending(next === "confidence");
    }
  }

  return (
    <>
      <Breadcrumbs items={[{ label: "Findings" }]} />
      <PageHeader eyebrow="Evidence review" title="Findings" description="Recorded observations with severity and extraction confidence shown independently." />
      <div className="finding-summary-row">
        <div className="finding-summary"><span className="summary-square summary-amber" /><span><strong>{records.filter((item) => item.finding.severity === "high").length}</strong><small>High severity</small></span></div>
        <div className="finding-summary"><span className="summary-square summary-purple" /><span><strong>{records.filter((item) => item.finding.observationStatus === "POTENTIAL_ANOMALY").length}</strong><small>Potential anomalies</small></span></div>
        <div className="finding-summary"><span className="summary-square summary-green" /><span><strong>{records.filter((item) => item.reviewStatus !== "UNREVIEWED").length}</strong><small>Reviewed</small></span></div>
      </div>
      <section className="panel filter-panel">
        <div className="filter-heading"><span><Icon name="filter" size={16} /> Filters</span><button className="text-button" onClick={() => {
          setCaseFilter("all"); setCategory("all"); setObservation("all"); setReview("all"); setSeverity("all"); setDocument("all"); setSearch("");
        }}>Reset</button></div>
        <div className="filter-grid">
          <label><span>Case</span><select value={activeCaseFilter} onChange={(event) => setCaseFilter(event.target.value)}><option value="all">All cases</option>{cases.map((item) => <option key={item.caseId} value={item.caseId}>{item.caseId}</option>)}</select></label>
          <label><span>Category</span><select value={category} onChange={(event) => setCategory(event.target.value)}><option value="all">All categories</option>{categories.map((item) => <option key={item} value={item}>{item.replaceAll("_", " ")}</option>)}</select></label>
          <label><span>Observation</span><select value={observation} onChange={(event) => setObservation(event.target.value)}><option value="all">All statuses</option>{["INCONSISTENT", "POTENTIAL_ANOMALY", "OBSERVED", "INCONCLUSIVE"].map((item) => <option key={item}>{item}</option>)}</select></label>
          <label><span>Review</span><select value={review} onChange={(event) => setReview(event.target.value)}><option value="all">All review states</option>{["UNREVIEWED", "CONFIRMED", "DISMISSED", "INCONCLUSIVE"].map((item) => <option key={item}>{item}</option>)}</select></label>
          <label><span>Severity</span><select value={severity} onChange={(event) => setSeverity(event.target.value)}><option value="all">All severities</option>{["high", "medium", "low", "info"].map((item) => <option key={item}>{item}</option>)}</select></label>
          <label><span>Document</span><select value={document} onChange={(event) => setDocument(event.target.value)}><option value="all">All documents</option>{docs.map((item) => <option key={item}>{item}</option>)}</select></label>
        </div>
        <div className="filter-search search-field"><Icon name="search" size={16} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search finding type or ID" aria-label="Search findings" /></div>
      </section>
      <div className="table-caption"><span>{filtered.length} of {records.length} recorded findings</span><span>{mockMode ? "Fixture results · not live analysis" : "Backend evidence store"}</span></div>
      {filtered.length === 0 ? <EmptyState title="No findings match these filters" detail="Adjust or reset the filters to see recorded fixture results." /> : (
        <div className="table-wrap">
          <table className="data-table finding-table">
            <thead><tr>
              <th aria-sort={sortKey === "type" ? descending ? "descending" : "ascending" : "none"}><SortButton label="Finding" active={sortKey === "type"} descending={descending} onClick={() => setSort("type")} /></th>
              <th aria-sort={sortKey === "category" ? descending ? "descending" : "ascending" : "none"}><SortButton label="Category" active={sortKey === "category"} descending={descending} onClick={() => setSort("category")} /></th>
              <th>Observation</th><th>Review state</th>
              <th aria-sort={sortKey === "severity" ? descending ? "descending" : "ascending" : "none"}><SortButton label="Severity" active={sortKey === "severity"} descending={descending} onClick={() => setSort("severity")} /></th>
              <th aria-sort={sortKey === "confidence" ? descending ? "descending" : "ascending" : "none"}><SortButton label="Confidence" active={sortKey === "confidence"} descending={descending} onClick={() => setSort("confidence")} /></th><th />
            </tr></thead>
            <tbody>{filtered.map((record) => <FindingRow key={record.finding.findingId} record={record} />)}</tbody>
          </table>
        </div>
      )}
    </>
  );
}


function FindingRow({ record }: { record: FindingWithReview }) {
  const { finding, reviewStatus } = record;
  const docs = Array.from(new Set(finding.evidence.flatMap((item) => item.regions.map((region) => region.docId))));
  return (
    <tr>
      <td><Link to={`/findings/${finding.findingId}`} className="finding-type-link"><strong>{finding.type.replaceAll("_", " ")}</strong><small>{finding.findingId} · {docs.join(", ")}</small></Link></td>
      <td><span className="category-text">{finding.category.replaceAll("_", " ")}</span></td>
      <td><ObservationLabel finding={finding} /><StatusBadge status={finding.observationStatus} /></td>
      <td><StatusBadge status={reviewStatus} /></td>
      <td><StatusBadge status={finding.severity} /></td>
      <td><Confidence value={finding.confidence} /></td>
      <td><Link to={`/findings/${finding.findingId}`} className="table-open" aria-label={`Open ${finding.type}`}><Icon name="chevron" size={16} /></Link></td>
    </tr>
  );
}

export function FindingDetailPage() {
  const { findingId = "" } = useParams();
  const viewerRef = useRef<DocumentViewerHandle>(null);
  const [selectedRegion, setSelectedRegion] = useState<Region | null>(null);
  const [documentId, setDocumentId] = useState("");
  const detailQuery = useQuery({ queryKey: ["finding", findingId], queryFn: () => api.finding(findingId) });
  const record = detailQuery.data;
  const finding = record?.finding;
  const regionOptions = finding?.evidence.flatMap((evidence) => evidence.regions) ?? [];
  const distinctEvidenceDocs = Array.from(new Set(regionOptions.map((region) => region.docId)));
  const selectedDocId = documentId || regionOptions[0]?.docId || "";
  const documentQuery = useQuery({
    queryKey: ["document", selectedDocId],
    queryFn: () => api.getDocument(selectedDocId),
    enabled: Boolean(selectedDocId),
  });

  if (detailQuery.isLoading) return <LoadingState label="Loading finding evidence" />;
  if (detailQuery.isError) return <ErrorState error={detailQuery.error} retry={() => void detailQuery.refetch()} />;
  if (!record || !finding) return <EmptyState title="Finding not available" detail={mockMode ? "This finding is not included in the recorded fixtures." : "This finding does not exist or belongs to an older reasoning run."} />;

  const selectedRegions = regionOptions.filter((region) => region.docId === selectedDocId);
  const focusedRegion = selectedRegion?.docId === selectedDocId ? selectedRegion : selectedRegions[0];
  const analysis = mockMode ? fixtureAnalysis[selectedDocId] : undefined;
  const leadEvidence = finding.evidence[0];
  return (
    <>
      <Breadcrumbs items={[{ label: "Findings", to: "/findings" }, { label: finding.findingId }]} />
      <PageHeader
        eyebrow={`${finding.findingId} · ${finding.category.replaceAll("_", " ")}`}
        title={finding.type.replaceAll("_", " ")}
        description={finding.explanation?.summary ?? leadEvidence?.description ?? "Recorded observation."}
        actions={distinctEvidenceDocs.length > 1 ? <Link className="button button-secondary" to={`/compare/${finding.findingId}`}><Icon name="swap" size={16} /> Compare regions</Link> : undefined}
      />
      <div className="detail-state-row">
        <div><small>Machine observation</small><StatusBadge status={finding.observationStatus} /><ObservationLabel finding={finding} /></div>
        <div><small>Analyst review</small><StatusBadge status={record.reviewStatus} /></div>
        <div><small>Severity</small><StatusBadge status={finding.severity} /></div>
        <div><small>Finding confidence</small><Confidence value={finding.confidence} /></div>
      </div>
      <div className="detail-layout">
        <div className="detail-main">
          <section className="panel evidence-panel">
            <div className="section-heading"><div><h2>Evidence location</h2><p>Regions use normalized coordinates on the canonical page render.</p></div>
              {regionOptions.length > 1 && <select className="compact-select" value={selectedDocId} onChange={(event) => { setDocumentId(event.target.value); setSelectedRegion(null); }} aria-label="Choose evidence document">
                {Array.from(new Set(regionOptions.map((region) => region.docId))).map((docId) => <option key={docId} value={docId}>{docId}</option>)}
              </select>}
            </div>
            {documentQuery.isError ? <ErrorState error={documentQuery.error} retry={() => void documentQuery.refetch()} /> : (
              <>
                <DocumentViewer
                  ref={viewerRef}
                  docId={selectedDocId}
                  title={documentQuery.data?.document.filename ?? selectedDocId}
                  fields={analysis?.fields ?? documentQuery.data?.fields ?? []}
                  signals={analysis?.signals ?? []}
                  findings={[finding]}
                  initialRegion={focusedRegion}
                  pageCount={documentQuery.data?.document.pageCount ?? analysis?.pages.length}
                />
                <div className="evidence-jump-list">
                  {selectedRegions.map((region, index) => (
                    <button className="evidence-jump" key={`${region.docId}-${region.label}-${index}`} aria-label={`Show in document: ${region.label ?? "evidence region"}, ${region.docId} page ${region.page}`} onClick={() => {
                      setSelectedRegion(region);
                      viewerRef.current?.focusRegion(region);
                    }}>
                      <span className="evidence-jump-marker">{index + 1}</span><span><strong>{region.label ?? "Evidence region"}</strong><small>{region.docId} · page {region.page}</small></span><span className="evidence-jump-action"><Icon name="pin" size={16} /> Show in document</span>
                    </button>
                  ))}
                </div>
              </>
            )}
          </section>
          <section className="panel explanation-panel">
            <div className="section-heading section-heading-tight"><div><p className="eyebrow">Structured explanation</p><h2>What was observed</h2></div></div>
            {finding.explanation ? (
              <>
                <div className="explanation-block"><h3>Observations</h3><ul>{finding.explanation.observations.map((item) => <li key={item}>{item}</li>)}</ul></div>
                <div className="explanation-columns">
                  <div className="explanation-block"><h3>Possible explanations</h3><ul>{finding.explanation.possibleExplanations.map((item) => <li key={item}>{item}</li>)}</ul></div>
                  <div className="explanation-block"><h3>Recommended review</h3><ul>{finding.explanation.recommendedReview.map((item) => <li key={item}>{item}</li>)}</ul></div>
                </div>
              </>
            ) : <p className="muted-copy">No structured explanation is recorded for this finding.</p>}
            {finding.needsVerification.length > 0 && <div className="verification-note"><strong>Needs verification</strong><ul>{finding.needsVerification.map((item) => <li key={item}>{item}</li>)}</ul></div>}
          </section>
          <section className="panel">
            <div className="section-heading section-heading-tight"><div><h2>Evidence records</h2><p>Provenance is specific to the recorded evidence.</p></div></div>
            <div className="evidence-record-list">{finding.evidence.map((evidence) => (
              <article className="evidence-record" key={evidence.evidenceId}>
                <div className="evidence-record-top"><span className="evidence-id">{evidence.evidenceId}</span><span className="kind-label">{evidence.kind.replaceAll("_", " ")}</span></div>
                <p>{evidence.description}</p>
                {evidence.assertionIds && <small>Assertions: {evidence.assertionIds.join(", ")}</small>}
                {evidence.measurements && <details><summary>Measurements and rule</summary><dl className="measurements">{Object.entries(evidence.measurements).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>)}</dl></details>}
                {Object.keys(evidence.provenance.config).length > 0 && <details><summary>Analyzer thresholds and configuration</summary><dl className="measurements">{Object.entries(evidence.provenance.config).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>)}</dl></details>}
                <div className="provenance-line"><span>{evidence.provenance.analyzer} v{evidence.provenance.analyzerVersion}</span><StatusBadge status={evidence.provenance.status} /><span>{new Date(evidence.provenance.processedAt).toLocaleString()}</span></div>
              </article>
            ))}</div>
          </section>
        </div>
        <aside className="detail-aside"><ReviewPanel detail={record} /></aside>
      </div>
    </>
  );
}

function ReviewPanel({ detail }: { detail: FindingDetail }) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const [comment, setComment] = useState("");
  const [validation, setValidation] = useState("");
  const mutation = useMutation({
    mutationFn: (body: ReviewRequest) => api.review(detail.finding.findingId, body),
    onMutate: async (body) => {
      await queryClient.cancelQueries({ queryKey: ["finding", detail.finding.findingId] });
      const previous = queryClient.getQueryData<FindingDetail>(["finding", detail.finding.findingId]);
      const optimistic: FindingDetail = {
        ...detail,
        reviewStatus: body.action === "CONFIRM" ? "CONFIRMED" : body.action === "DISMISS" ? "DISMISSED" : body.action === "INCONCLUSIVE" ? "INCONCLUSIVE" : body.action === "REOPEN" ? "UNREVIEWED" : detail.reviewStatus,
      };
      queryClient.setQueryData(["finding", detail.finding.findingId], optimistic);
      return { previous };
    },
    onError: (error, _body, context) => {
      if (context?.previous) queryClient.setQueryData(["finding", detail.finding.findingId], context.previous);
      setValidation(error instanceof Error ? error.message : "The review action was not saved.");
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(["finding", detail.finding.findingId], updated);
      setReason("");
      setComment("");
      setValidation("");
    },
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: ["findings"] });
      await queryClient.invalidateQueries({ queryKey: ["events"] });
      await queryClient.invalidateQueries({ queryKey: ["dossier"] });
    },
  });

  function submit(body: ReviewRequest) {
    setValidation("");
    if (body.action === "DISMISS" && !body.reason?.trim()) {
      setValidation("Add a reason before dismissing this observation.");
      return;
    }
    if (body.action === "COMMENT" && !body.comment?.trim()) {
      setValidation("Add a comment before posting.");
      return;
    }
    mutation.mutate(body);
  }

  return (
    <section className="panel review-panel">
      <div className="review-panel-heading"><span className="review-heading-icon"><Icon name="user" /></span><div><h2>Analyst review</h2><p>Review actions are recorded separately from machine output.</p></div></div>
      <div className="review-status-pair"><div><small>Observation</small><StatusBadge status={detail.finding.observationStatus} /></div><div><small>Review state</small><StatusBadge status={detail.reviewStatus} /></div></div>
      <>
      <div className="review-actions">
        <Button onClick={() => submit({ action: "CONFIRM" })} disabled={mutation.isPending}><Icon name="check" size={16} /> Confirm observation</Button>
        <label className="field-label" htmlFor="dismiss-reason">Reason for dismissal</label>
        <textarea id="dismiss-reason" className="text-input textarea" value={reason} onChange={(event) => setReason(event.target.value)} rows={3} placeholder="Required to dismiss" />
        <Button variant="danger" onClick={() => submit({ action: "DISMISS", reason })} disabled={mutation.isPending}>Dismiss with reason</Button>
        <Button variant="secondary" onClick={() => submit({ action: "INCONCLUSIVE" })} disabled={mutation.isPending}>Mark inconclusive</Button>
        {detail.reviewStatus !== "UNREVIEWED" && <Button variant="quiet" onClick={() => submit({ action: "REOPEN" })} disabled={mutation.isPending}>Reopen review</Button>}
      </div>
      <div className="comment-box"><label className="field-label" htmlFor="review-comment">Add a comment</label><textarea id="review-comment" className="text-input textarea" value={comment} onChange={(event) => setComment(event.target.value)} rows={2} placeholder="Record context for the review history" /><Button variant="secondary" onClick={() => submit({ action: "COMMENT", comment })} disabled={mutation.isPending}>Add comment</Button></div>
      </>
      {validation && <p className="form-error" role="alert">{validation}</p>}
      <div className="review-history"><div className="section-heading section-heading-tight"><h3>Review history</h3><span>{detail.reviewHistory.length} events</span></div>
        {detail.reviewHistory.length === 0 ? <p className="history-empty">No review activity recorded yet.</p> : (
          <ol className="timeline">{detail.reviewHistory.map((event) => (
            <li key={event.eventId}><span className="timeline-dot" /><div><strong>{reviewEventLabel(event)}</strong><small>{event.actor} · {new Date(event.at).toLocaleString()}</small>{typeof event.payload.reason === "string" && <p>{event.payload.reason}</p>}{typeof event.payload.text === "string" && <p>{event.payload.text}</p>}</div></li>
          ))}</ol>
        )}
      </div>
    </section>
  );
}

export function ComparePage() {
  const { findingId = "" } = useParams();
  const navigate = useNavigate();
  const [swapped, setSwapped] = useState(false);
  const [syncZoom, setSyncZoom] = useState(true);
  const [compareZoom, setCompareZoom] = useState(1);
  const findingQuery = useQuery({ queryKey: ["finding", findingId], queryFn: () => api.finding(findingId) });
  const finding = findingQuery.data?.finding;
  const assertionQuery = useQuery({
    queryKey: ["assertions", finding?.caseId],
    queryFn: () => api.assertions(finding?.caseId ?? ""),
    enabled: Boolean(finding?.caseId),
  });
  const regions = finding?.evidence.flatMap((evidence) => evidence.regions) ?? [];
  const distinctDocs = Array.from(new Set(regions.map((region) => region.docId)));
  const leftDoc = distinctDocs[swapped ? 1 : 0] ?? distinctDocs[0] ?? "";
  const rightDoc = distinctDocs[swapped ? 0 : 1] ?? distinctDocs[0] ?? "";
  const leftAnalysis = mockMode ? fixtureAnalysis[leftDoc] : undefined;
  const rightAnalysis = mockMode ? fixtureAnalysis[rightDoc] : undefined;

  if (findingQuery.isLoading || assertionQuery.isLoading) return <LoadingState label="Preparing evidence comparison" />;
  if (findingQuery.isError) return <ErrorState error={findingQuery.error} retry={() => void findingQuery.refetch()} />;
  if (!finding) return <EmptyState title="Finding not available" detail={mockMode ? "This finding is not included in the recorded fixtures." : "This finding does not exist or belongs to an older reasoning run."} />;

  const measurements = finding.evidence[0]?.measurements ?? {};
  if (distinctDocs.length < 2) {
    return <EmptyState title="Cross-document comparison unavailable" detail="This finding does not include evidence from two distinct documents." />;
  }
  const assertionIds = new Set(finding.evidence.flatMap((evidence) => evidence.assertionIds ?? []));
  const assertions = assertionQuery.data?.assertions ?? [];
  const leftAssertion = assertions.find((assertion) => assertion.docId === leftDoc && assertionIds.has(assertion.assertionId));
  const rightAssertion = assertions.find((assertion) => assertion.docId === rightDoc && assertionIds.has(assertion.assertionId));
  return (
    <>
      <Breadcrumbs items={[{ label: "Findings", to: "/findings" }, { label: finding.findingId, to: `/findings/${finding.findingId}` }, { label: "Compare" }]} />
      <PageHeader eyebrow="Evidence comparison" title="Compare regions" description="Both panels use the recorded canonical render and evidence coordinates." actions={<div className="compare-actions">{distinctDocs.length > 1 && <Button variant="secondary" onClick={() => setSwapped((value) => !value)}><Icon name="swap" size={16} /> Swap panels</Button>}<Button variant="secondary" aria-pressed={syncZoom} onClick={() => setSyncZoom((value) => !value)}>Sync zoom {syncZoom ? "on" : "off"}</Button><Button variant="quiet" onClick={() => navigate(`/findings/${finding.findingId}`)}>Back to finding</Button></div>} />
      <div className="compare-rule-banner"><div><span className="rule-icon"><Icon name="swap" /></span><div><strong>{finding.type.replaceAll("_", " ")}</strong><p>{finding.evidence[0]?.description ?? "Recorded comparison rule."}</p></div></div><div className="compare-rule-meta"><span>Rule <strong>{String(measurements.ruleId ?? measurements.ruleType ?? "Not recorded")}</strong></span><span>Entity resolution <strong>{String(measurements.entityResolution ?? "Not recorded")}</strong></span><span>Legitimate difference check <strong>{String(measurements.legitimateDifferenceRules ?? "Not recorded")}</strong></span></div></div>
      {assertionQuery.isError && <ErrorState error={assertionQuery.error} retry={() => void assertionQuery.refetch()} />}
      <div className="value-compare-row">{[{ docId: leftDoc, assertion: leftAssertion }, { docId: rightDoc, assertion: rightAssertion }].map(({ docId, assertion }, index) => (
        <div key={`${docId}-${index}`}><small>{docId}</small><strong>{assertion ? assertion.normalizedValue ?? "Value unavailable" : assertionQuery.isError ? "Assertion data unavailable" : "No extracted assertion recorded"}</strong>{assertion ? <Confidence value={assertion.extractionConfidence} /> : <span className="muted-copy">Extraction confidence unavailable</span>}</div>
      ))}</div>
      <div className="compare-viewers">
        <DocumentViewer docId={leftDoc} title={leftDoc} fields={leftAnalysis?.fields ?? []} signals={leftAnalysis?.signals ?? []} findings={[finding]} initialRegion={regions.find((region) => region.docId === leftDoc)} zoom={syncZoom ? compareZoom : undefined} onZoomChange={syncZoom ? setCompareZoom : undefined} pageCount={leftAnalysis?.pages.length} />
        <DocumentViewer docId={rightDoc} title={rightDoc} fields={rightAnalysis?.fields ?? []} signals={rightAnalysis?.signals ?? []} findings={[finding]} initialRegion={regions.find((region) => region.docId === rightDoc)} zoom={syncZoom ? compareZoom : undefined} onZoomChange={syncZoom ? setCompareZoom : undefined} pageCount={rightAnalysis?.pages.length} />
      </div>
      <div className="compare-note"><Icon name="shield" size={17} /> Difference alone is not a conclusion. Refer to the documented rule, entity resolution and review status.</div>
    </>
  );
}

/** Label for a review-history row. The backend records REVIEW {status}, COMMENT {text} and REOPEN {previousStatus}. */
function reviewEventLabel(event: InvestigationEvent): string {
  if (event.type === "REVIEW" && typeof event.payload.status === "string") return event.payload.status;
  return event.type;
}
