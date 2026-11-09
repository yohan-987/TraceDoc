import { useMemo, useState } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import type { CaseSummary, DocumentDetail, EventsResponse, FindingWithReview } from "../../../contracts/api";
import type { DocumentRecord, Job, JobStatus, PriorityResult } from "../../../contracts/types";
import { api } from "../api/client";
import { Chip, Skeleton, SkeletonPanel, StatCard } from "../components/Primitives";
import { bandTone } from "../components/Ring";
import { EmptyState, ErrorState, Icon, PageHeader, StatusBadge } from "../components/ui";
import { mockMode, pageRenderUrl } from "../config";
import { fixturePageUrls } from "../mockData";
import { downloadFindingsCsv, downloadResultsJson, type ExportDocument } from "../utils/exportFindings";
import { ANALYZER_IDS, DOC_TYPE_LABEL, analyzerCoverage, bandLabel, findingsForDocument, humanize, observationLabel, relativeTime, severityRank } from "../lib/format";

type Filter = "all" | "needs-review" | "no-findings" | "partial" | "failed";
type Sort = "date" | "findings" | "priority";

const FILTERS: Array<{ key: Filter; label: string }> = [
  { key: "all", label: "All" },
  { key: "needs-review", label: "Needs review" },
  { key: "no-findings", label: "No findings observed" },
  { key: "partial", label: "Partial results" },
  { key: "failed", label: "Failed" },
];
const REVIEW_EVENTS = new Set(["REVIEW", "COMMENT", "REOPEN"]);

export interface DocRow {
  doc: DocumentRecord;
  caseTitle: string;
  casePriority: PriorityResult | null;
  status: JobStatus;
  findings: FindingWithReview[];
  openCount: number;
  ran: number;
  possible: number;
  lastReviewed: string | null;
  types: string[] | null;
}

/** Status of a document: its most recent job, or, with no job on record, inferred from its analyzer statuses. */
export function documentStatus(doc: DocumentRecord, jobs: Job[]): JobStatus {
  const latest = jobs.filter((job) => job.docId === doc.docId).sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))[0];
  if (latest) return latest.status;
  const statuses = Object.values(doc.processing);
  return statuses.some((status) => status === "UNAVAILABLE" || status === "ERROR") ? "PARTIAL" : "DONE";
}

export function matchesFilter(row: DocRow, filter: Filter): boolean {
  switch (filter) {
    case "needs-review": return row.openCount > 0;
    case "no-findings": return row.findings.length === 0 && row.ran > 0 && row.status !== "FAILED";
    case "partial": return row.status === "PARTIAL";
    case "failed": return row.status === "FAILED";
    default: return true;
  }
}

export function matchesSearch(row: DocRow, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [row.doc.filename, row.doc.docId, row.doc.sha256, row.doc.caseId, row.caseTitle].some((value) => value.toLowerCase().includes(needle));
}

export function sortRows(rows: DocRow[], sort: Sort): DocRow[] {
  const copy = [...rows];
  if (sort === "findings") return copy.sort((a, b) => b.openCount - a.openCount || b.findings.length - a.findings.length);
  if (sort === "priority") return copy.sort((a, b) => (b.casePriority?.score ?? -1) - (a.casePriority?.score ?? -1));
  return copy.sort((a, b) => Date.parse(b.doc.receivedAt) - Date.parse(a.doc.receivedAt));
}

function thumbnailUrl(docId: string): string | undefined {
  return mockMode ? fixturePageUrls[docId] : pageRenderUrl(docId, 1);
}

export function HomePage() {
  const [params, setParams] = useSearchParams();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const filter = (FILTERS.some((item) => item.key === params.get("filter")) ? params.get("filter") : "all") as Filter;
  const sort = (["date", "findings", "priority"].includes(params.get("sort") ?? "") ? params.get("sort") : "date") as Sort;
  const query = params.get("q") ?? "";
  const grouped = params.get("group") === "1";
  function setParam(key: string, value: string | null) {
    setParams((current) => {
      const next = new URLSearchParams(current);
      if (value === null || value === "" || (key === "filter" && value === "all") || (key === "sort" && value === "date")) next.delete(key);
      else next.set(key, value);
      return next;
    }, { replace: true });
  }

  const summaries = useQuery({ queryKey: ["case-summaries"], queryFn: api.listCaseSummaries, refetchOnWindowFocus: "always" });
  const caseList: CaseSummary[] = summaries.data ?? [];
  const findingQueries = useQueries({
    queries: caseList.map((summary) => ({ queryKey: ["findings", "case", summary.case.caseId], queryFn: () => api.findings(summary.case.caseId), refetchOnWindowFocus: "always" as const })),
  });
  const eventQueries = useQueries({
    queries: caseList.map((summary) => ({ queryKey: ["events", summary.case.caseId], queryFn: () => api.events(summary.case.caseId), refetchOnWindowFocus: "always" as const })),
  });
  const documents = caseList.flatMap((summary) => summary.documents);
  const detailQueries = useQueries({
    queries: documents.map((doc) => ({ queryKey: ["document", doc.docId], queryFn: () => api.getDocument(doc.docId) })),
  });

  const rows = useMemo<DocRow[]>(() => {
    const detailById = new Map<string, DocumentDetail | undefined>(documents.map((doc, index) => [doc.docId, detailQueries[index]?.data]));
    return caseList.flatMap((summary, caseIndex) => {
      const findings = findingQueries[caseIndex]?.data?.findings ?? [];
      const events: EventsResponse["events"] = eventQueries[caseIndex]?.data?.events ?? [];
      return summary.documents.map((doc) => {
        const own = findingsForDocument(findings, doc.docId);
        const ids = new Set(own.map((item) => item.finding.findingId));
        const reviewed = events.filter((event) => REVIEW_EVENTS.has(event.type) && ids.has(event.targetId)).map((event) => event.at).sort().pop() ?? null;
        const coverage = analyzerCoverage(doc.processing);
        const pages = detailById.get(doc.docId)?.pages;
        return {
          doc,
          caseTitle: summary.case.title,
          casePriority: summary.priority,
          status: documentStatus(doc, summary.jobs),
          findings: own,
          openCount: own.filter((item) => item.reviewStatus === "UNREVIEWED" || item.reviewStatus === "INCONCLUSIVE").length,
          ran: coverage.ran,
          possible: coverage.possible,
          lastReviewed: reviewed,
          types: pages ? Array.from(new Set(pages.map((page) => page.docType))) : null,
        };
      });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseList, findingQueries.map((item) => item.dataUpdatedAt).join(), eventQueries.map((item) => item.dataUpdatedAt).join(), detailQueries.map((item) => item.dataUpdatedAt).join()]);

  const visible = useMemo(() => sortRows(rows.filter((row) => matchesFilter(row, filter) && matchesSearch(row, query)), sort), [rows, filter, query, sort]);
  const selected = rows.find((row) => row.doc.docId === selectedId) ?? null;
  const findingsLoading = findingQueries.some((item) => item.isLoading);

  const action = <Link className="button button-primary scan-button" to="/scanner"><Icon name="scanner" size={16} /> Scan new document</Link>;

  if (summaries.isLoading) {
    return (
      <>
        <PageHeader eyebrow="Overview" title="Analysed documents" actions={action} />
        <div className="stat-grid" aria-busy="true">{[0, 1, 2, 3].map((index) => <div className="stat-card" key={index}><Skeleton width="50%" height={10} /><Skeleton width="35%" height={30} /></div>)}</div>
        <SkeletonPanel rows={6} label="Loading analysed documents" />
      </>
    );
  }
  if (summaries.isError) return <ErrorState error={summaries.error} retry={() => void summaries.refetch()} />;

  const exportDocs: ExportDocument[] = rows.map((row) => ({ docId: row.doc.docId, caseId: row.doc.caseId, caseTitle: row.caseTitle, filename: row.doc.filename, status: row.status, analyzersRan: row.ran, analyzersPossible: row.possible, findings: row.findings }));
  const withFindings = rows.filter((row) => row.findings.length > 0).length;
  const partialOrFailed = rows.filter((row) => row.status === "PARTIAL" || row.status === "FAILED" || row.casePriority?.incompleteCoverage === true).length;

  return (
    <>
      <PageHeader eyebrow="Overview" title="Analysed documents" description="Every document the analyzers have processed. Findings are observations for human review, never verdicts." actions={action} />
      <div className="stat-grid">
        <StatCard icon="documents" label="Documents analysed" value={rows.length} hint={`across ${caseList.length} ${caseList.length === 1 ? "case" : "cases"}`} />
        <StatCard icon="findings" label="With findings" value={findingsLoading ? "…" : withFindings} hint="at least one observation recorded" tone={withFindings > 0 ? "amber" : "green"} />
        <StatCard icon="scanner" label="Partial or failed" value={partialOrFailed} hint="not every expected analyzer ran" tone={partialOrFailed > 0 ? "red" : "green"} />
        <StatCard icon="cases" label="Cases" value={caseList.length} hint="documents are grouped into cases" to="/cases" />
      </div>

      {rows.length === 0 ? (
        <EmptyState
          title="No documents analysed yet"
          detail="Scan a document to see what the analyzers observe. You can start with the bundled sample documents."
          action={<Link className="button button-primary" to="/scanner?samples=1">Use sample documents</Link>}
        />
      ) : (
        <div className={`home-layout ${selected ? "home-layout-split" : ""}`}>
          <section className="panel home-list" aria-label="Analysed documents">
            <div className="home-toolbar">
              <div className="chip-row" role="group" aria-label="Filter documents">
                {FILTERS.map((item) => <button key={item.key} type="button" className="chip" aria-pressed={filter === item.key} onClick={() => setParam("filter", item.key)}>{item.label}</button>)}
              </div>
              <div className="home-controls">
                <label className="inline-search"><Icon name="search" size={14} /><input type="search" aria-label="Search documents" placeholder="Filename, hash or case" value={query} onChange={(event) => setParam("q", event.target.value)} /></label>
                <label className="inline-select"><span className="label">Sort</span>
                  <select aria-label="Sort documents" value={sort} onChange={(event) => setParam("sort", event.target.value)}>
                    <option value="date">Date</option><option value="findings">Findings</option><option value="priority">Priority</option>
                  </select>
                </label>
                <span className="export-group" role="group" aria-label="Export all findings and results">
                  <button type="button" className="button button-secondary export-button" disabled={findingsLoading} title="Every finding across all cases, one row per finding" onClick={() => downloadFindingsCsv(exportDocs, "tracedocs")}><Icon name="download" size={14} /> Export CSV</button>
                  <button type="button" className="button button-secondary export-button" disabled={findingsLoading} title="Documents, findings with evidence and review status" onClick={() => downloadResultsJson(exportDocs, mockMode ? "fixture" : "live", "tracedocs")}><Icon name="download" size={14} /> Export JSON</button>
                </span>
                <button type="button" className="chip" aria-pressed={grouped} onClick={() => setParam("group", grouped ? null : "1")}>Group by case</button>
              </div>
            </div>
            <p className="result-count" role="status">{visible.length} of {rows.length} documents</p>
            {visible.length === 0 ? (
              <EmptyState title="No documents match" detail="Clear the search or choose a different filter." action={<button type="button" className="button button-secondary" onClick={() => setParams({}, { replace: true })}>Reset filters</button>} />
            ) : (
              <div className="table-scroll">
                <table className="data-table doc-table">
                  <thead>
                    <tr>
                      <th scope="col"><span className="sr-only">Preview</span></th><th scope="col">Document</th><th scope="col">Case</th><th scope="col">Type</th><th scope="col">Uploaded</th>
                      <th scope="col">Status</th><th scope="col">Analyzers ran</th><th scope="col">Open findings</th><th scope="col">Case priority <span className="scope-tag">Case-wide</span></th><th scope="col">Last reviewed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(grouped ? groupByCase(visible) : [{ caseId: "", title: "", items: visible }]).flatMap((group) => [
                      ...(grouped ? [<tr key={`g-${group.caseId}`} className="group-row"><th colSpan={10} scope="colgroup">{group.caseId} · {group.title}</th></tr>] : []),
                      ...group.items.map((row) => <DocTableRow key={row.doc.docId} row={row} selected={row.doc.docId === selectedId} onSelect={() => setSelectedId(row.doc.docId)} loadingFindings={findingsLoading} />),
                    ])}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          {selected && <PreviewPanel row={selected} onClose={() => setSelectedId(null)} />}
        </div>
      )}
      <ol className="flow-footer" aria-label="Review workflow"><li>Scan</li><li>Analyse</li><li>Investigate</li><li>Review findings</li><li>Report</li></ol>
    </>
  );
}

function groupByCase(rows: DocRow[]) {
  const groups = new Map<string, { caseId: string; title: string; items: DocRow[] }>();
  rows.forEach((row) => {
    const group = groups.get(row.doc.caseId) ?? { caseId: row.doc.caseId, title: row.caseTitle, items: [] };
    group.items.push(row);
    groups.set(row.doc.caseId, group);
  });
  return [...groups.values()];
}

function Thumb({ docId, name }: { docId: string; name: string }) {
  const url = thumbnailUrl(docId);
  return url ? <img className="doc-thumb" src={url} alt={`${name}, page 1`} loading="lazy" /> : <span className="doc-thumb doc-thumb-empty" role="img" aria-label="No preview available"><Icon name="file" size={16} /></span>;
}

function DocTableRow({ row, selected, onSelect, loadingFindings }: { row: DocRow; selected: boolean; onSelect: () => void; loadingFindings: boolean }) {
  const { doc } = row;
  return (
    <tr className={selected ? "row-selected" : ""} onClick={onSelect} data-docid={doc.docId}>
      <td><Thumb docId={doc.docId} name={doc.filename} /></td>
      <td>
        <button type="button" className="row-link" aria-pressed={selected} aria-label={`Preview ${doc.filename}`} onClick={(event) => { event.stopPropagation(); onSelect(); }}>{doc.filename}</button>
        <small className="mono-value">{doc.docId} · {doc.sha256.slice(0, 12)}…</small>
      </td>
      <td><span className="mono-value">{doc.caseId}</span></td>
      <td>{row.types === null ? <Skeleton width={70} /> : row.types.length === 0 ? "—" : row.types.map((type) => DOC_TYPE_LABEL[type] ?? type).join(", ")}</td>
      <td title={doc.receivedAt}>{relativeTime(doc.receivedAt)}</td>
      <td><StatusBadge status={row.status} /></td>
      <td><span className="mono-value">{row.ran} / {row.possible}</span>{row.casePriority?.incompleteCoverage && <small className="muted-text"> incomplete</small>}</td>
      <td>
        {loadingFindings ? <Skeleton width={30} /> : (
          <>
            <strong>{row.openCount}</strong>
            {row.findings.length === 0 && <small className="cell-note">{row.ran > 0 ? "No anomalies observed by the analyzers that ran" : "No analyzer ran, so this is not a clean result"}</small>}
          </>
        )}
      </td>
      <td>{row.casePriority ? <Chip tone={bandTone(row.casePriority.priorityBand)}>{bandLabel[row.casePriority.priorityBand]}</Chip> : <Chip tone="grey">Pending</Chip>}</td>
      <td>{row.lastReviewed ? relativeTime(row.lastReviewed) : "—"}</td>
    </tr>
  );
}

function PreviewPanel({ row, onClose }: { row: DocRow; onClose: () => void }) {
  const top = [...row.findings].sort((a, b) => severityRank(b.finding.severity) - severityRank(a.finding.severity) || b.finding.confidence - a.finding.confidence).slice(0, 3);
  return (
    <aside className="panel preview-panel" aria-label={`Preview of ${row.doc.filename}`}>
      <div className="preview-head">
        <div><p className="label">Selected document</p><h2>{row.doc.filename}</h2><span className="mono-value">{row.doc.docId}</span></div>
        <button type="button" className="icon-button" aria-label="Close preview" onClick={onClose}><Icon name="close" size={16} /></button>
      </div>
      <div className="preview-row"><StatusBadge status={row.status} /><span className="mono-value">{row.ran} of {row.possible} reported analyzers ran{row.casePriority?.incompleteCoverage ? ` · ${row.casePriority.missingAnalyzers?.length ?? 0} expected analyzers not installed` : ""}</span></div>
      <section>
        <p className="label">Top observations</p>
        {top.length === 0 ? (
          <p className="muted-text">{row.ran > 0 ? "No anomalies observed by the analyzers that ran." : "No analyzer ran, so absence of findings is not a clean result."}</p>
        ) : (
          <ol className="why-list">
            {top.map((item, index) => (
              <li key={item.finding.findingId}>
                <span className="why-index">{String(index + 1).padStart(2, "0")}</span>
                <span><strong>{humanize(item.finding.type)}</strong><small>{observationLabel(item.finding.observationStatus)} · {Math.round(item.finding.confidence * 100)}% confidence</small></span>
              </li>
            ))}
          </ol>
        )}
      </section>
      <section>
        <p className="label">Analyzer status</p>
        <ul className="status-list">
          {ANALYZER_IDS.filter((id) => id in row.doc.processing).map((id) => <li key={id}><span className="mono">{id}</span><StatusBadge status={row.doc.processing[id]} /></li>)}
        </ul>
      </section>
      <p className="muted-text">Case priority is <strong>case-wide</strong>: it combines every document in {row.doc.caseId}.</p>
      <Link className="button button-primary" to={`/documents/${encodeURIComponent(row.doc.docId)}/overview`}>INSPECT <Icon name="arrow" size={15} /></Link>
    </aside>
  );
}
