import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import type { UploadResponse } from "../../../contracts/api";
import type { DocumentRecord, Job } from "../../../contracts/types";
import { api } from "../api/client";
import { Dock, buildDockTabs } from "../components/Dock";
import { Chip } from "../components/Primitives";
import { Gauge, Ring } from "../components/Ring";
import { AnalyzerChecklist, StepProgress } from "../components/Stepper";
import { Button, ErrorState, Icon, PageHeader, StatusBadge } from "../components/ui";
import { jobPollIntervalMs, maxUploadMegabytes, mockMode } from "../config";
import { findingsForDocument, formatBytes, humanize, observationLabel, pct, severityRank, toHex } from "../lib/format";
import { isTerminal, jobCoverage, pollDelay, settledCount } from "../lib/jobs";

const ACCEPTED = new Set(["application/pdf", "image/png", "image/jpeg", "image/tiff"]);
const ACCEPTED_EXTENSIONS = /\.(pdf|png|jpe?g|tiff?)$/i;
const MAX_BYTES = maxUploadMegabytes * 1024 * 1024;
const SAMPLES = [
  { name: "D-01.png", type: "image/png" },
  { name: "D-02.png", type: "image/png" },
  { name: "D-03.pdf", type: "application/pdf" },
];
const AUTO_OPEN_KEY = "tracedocs.autoOpen";

interface Picked { file: File; sha256: string | null | "pending" }

function readBuffer(file: File): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(file);
  });
}

/** SHA-256 of the file as read in the browser; null where the Web Crypto API is unavailable (it needs a secure context). */
export async function sha256Hex(file: File): Promise<string | null> {
  try {
    if (!globalThis.crypto?.subtle) return null;
    return toHex(await globalThis.crypto.subtle.digest("SHA-256", await readBuffer(file)));
  } catch {
    return null;
  }
}

export function ScannerPage() {
  const [picked, setPicked] = useState<Picked[]>([]);
  const [errors, setErrors] = useState<string[]>([]);
  const [dragging, setDragging] = useState(false);
  const [caseId, setCaseId] = useState("");
  const [creatingCase, setCreatingCase] = useState(false);
  const [newCaseTitle, setNewCaseTitle] = useState("");
  const [upload, setUpload] = useState<UploadResponse | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [activeDoc, setActiveDoc] = useState(0);
  const [loadingSamples, setLoadingSamples] = useState(false);
  const [autoOpen, setAutoOpen] = useState(() => sessionStorage.getItem(AUTO_OPEN_KEY) === "1");
  const inputRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const queryClient = useQueryClient();

  const casesQuery = useQuery({ queryKey: ["cases"], queryFn: api.listCases });
  useEffect(() => { if (!caseId && casesQuery.data?.[0]) setCaseId(casesQuery.data[0].caseId); }, [caseId, casesQuery.data]);

  const createCase = useMutation({
    mutationFn: () => api.createCase({ title: newCaseTitle.trim() }),
    onSuccess: async (created) => {
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
      await queryClient.invalidateQueries({ queryKey: ["case-summaries"] });
      setCaseId(created.caseId);
      setNewCaseTitle("");
      setCreatingCase(false);
    },
  });

  const uploadMutation = useMutation({
    mutationFn: () => api.upload(caseId, picked.map((item) => item.file), ({ percent }) => setProgress(percent)),
    onSuccess: async (result) => {
      setUpload(result);
      setActiveDoc(0);
      setProgress(null);
      setErrors([]);
      await Promise.all([["cases"], ["case-summaries"], ["findings"], ["events"]].map((queryKey) => queryClient.invalidateQueries({ queryKey })));
    },
    onError: (error) => {
      setProgress(null);
      setErrors([error instanceof Error ? error.message : "Upload could not be completed."]);
    },
  });

  function addFiles(incoming: FileList | File[]) {
    const accepted: File[] = [];
    const rejected: string[] = [];
    Array.from(incoming).forEach((file) => {
      if (!(ACCEPTED.has(file.type) || ACCEPTED_EXTENSIONS.test(file.name))) rejected.push(`${file.name}: file type is not supported.`);
      else if (file.size > MAX_BYTES) rejected.push(`${file.name}: exceeds the ${maxUploadMegabytes} MB limit.`);
      else accepted.push(file);
    });
    const fresh = accepted.filter((file) => !picked.some((item) => item.file.name === file.name && item.file.size === file.size));
    setErrors(rejected);
    setPicked((existing) => [...existing, ...fresh.map((file) => ({ file, sha256: "pending" as const }))]);
    fresh.forEach((file) => {
      void sha256Hex(file).then((hash) => setPicked((existing) => existing.map((item) => item.file === file ? { ...item, sha256: hash } : item)));
    });
  }

  async function useSamples() {
    setLoadingSamples(true);
    try {
      const files = await Promise.all(SAMPLES.map(async (sample) => {
        const response = await fetch(`/samples/${sample.name}`);
        if (!response.ok) throw new Error(`${sample.name} could not be loaded.`);
        return new File([await response.blob()], sample.name, { type: sample.type });
      }));
      addFiles(files);
    } catch (error) {
      setErrors([error instanceof Error ? error.message : "The sample documents could not be loaded."]);
    } finally {
      setLoadingSamples(false);
    }
  }

  const samplesRequested = params.get("samples") === "1";
  const samplesStarted = useRef(false);
  useEffect(() => {
    if (samplesRequested && !samplesStarted.current) { samplesStarted.current = true; void useSamples(); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [samplesRequested]);

  /* ---- analysis: poll every job with backoff; a failed document never blocks the others ---- */
  const jobQueries = useQueries({
    queries: (upload?.jobs ?? []).map((initial) => ({
      queryKey: ["job", initial.jobId],
      queryFn: () => api.getJob(initial.jobId),
      initialData: initial,
      refetchInterval: (query: { state: { data: Job | undefined; dataUpdateCount: number } }) =>
        isTerminal(query.state.data?.status) ? false : pollDelay(jobPollIntervalMs, query.state.dataUpdateCount),
    })),
  });
  const jobs: Job[] = jobQueries.map((query, index) => query.data ?? upload!.jobs[index]);
  const documents: DocumentRecord[] = upload?.documents ?? [];
  const allTerminal = jobs.length > 0 && jobs.every((job) => isTerminal(job.status));
  const step = !upload ? 0 : allTerminal ? 2 : 1;

  const caseIds = useMemo(() => Array.from(new Set(documents.map((doc) => doc.caseId))), [documents]);
  const resultQueries = useQueries({
    queries: caseIds.flatMap((id) => [
      { queryKey: ["case", id], queryFn: () => api.getCase(id), enabled: allTerminal },
      { queryKey: ["findings", "case", id], queryFn: () => api.findings(id), enabled: allTerminal },
    ]),
  });
  const summaryFor = (id: string) => resultQueries[caseIds.indexOf(id) * 2]?.data as Awaited<ReturnType<typeof api.getCase>> | undefined;
  const findingsFor = (id: string) => (resultQueries[caseIds.indexOf(id) * 2 + 1]?.data as Awaited<ReturnType<typeof api.findings>> | undefined)?.findings ?? [];
  const resultsReady = allTerminal && resultQueries.every((query) => !query.isLoading);

  const firstOpenable = jobs.findIndex((job) => job.status !== "FAILED");
  useEffect(() => {
    if (autoOpen && resultsReady && firstOpenable >= 0) navigate(`/documents/${encodeURIComponent(documents[firstOpenable].docId)}/overview`);
  }, [autoOpen, resultsReady, firstOpenable, documents, navigate]);

  function reset() {
    setUpload(null); setPicked([]); setErrors([]); setActiveDoc(0);
  }

  if (casesQuery.isError) return <ErrorState error={casesQuery.error} retry={() => void casesQuery.refetch()} />;

  return (
    <>
      <PageHeader eyebrow="Document scanner" title="Scan documents" description="Upload documents, watch each analyzer report, then open the investigation. Results are observations for human review." />
      <StepProgress steps={["Upload", "Analyze", "Result"]} current={step} />
      <section className="panel scanner-card" data-step={["upload", "analyze", "result"][step]}>
        <div className="step-view" key={step}>
          {step === 0 && (
            <UploadStep
              picked={picked} errors={errors} dragging={dragging} setDragging={setDragging} addFiles={addFiles} inputRef={inputRef}
              remove={(index) => setPicked((items) => items.filter((_, itemIndex) => itemIndex !== index))}
              cases={casesQuery.data ?? []} casesLoading={casesQuery.isLoading} caseId={caseId} setCaseId={setCaseId}
              creatingCase={creatingCase} setCreatingCase={setCreatingCase} newCaseTitle={newCaseTitle} setNewCaseTitle={setNewCaseTitle}
              createCase={() => createCase.mutate()} createPending={createCase.isPending} createError={createCase.isError ? (createCase.error instanceof Error ? createCase.error.message : "Could not create case.") : ""}
              useSamples={() => void useSamples()} loadingSamples={loadingSamples}
              submit={() => uploadMutation.mutate()} uploading={uploadMutation.isPending} progress={progress}
            />
          )}
          {step === 1 && <AnalyzeStep jobs={jobs} documents={documents} active={activeDoc} setActive={setActiveDoc} />}
          {step === 2 && (
            <ResultStep
              jobs={jobs} documents={documents} active={activeDoc} setActive={setActiveDoc} ready={resultsReady}
              summaryFor={summaryFor} findingsFor={findingsFor} autoOpen={autoOpen}
              setAutoOpen={(value) => { setAutoOpen(value); sessionStorage.setItem(AUTO_OPEN_KEY, value ? "1" : "0"); }}
              reset={reset}
            />
          )}
        </div>
      </section>
      {step === 2 && documents[activeDoc] && (
        <>
          <div className="doc-switcher" role="group" aria-label="Uploaded documents">
            {documents.map((doc, index) => (
              <button key={doc.docId} type="button" className="chip" aria-pressed={index === activeDoc} onClick={() => setActiveDoc(index)}>
                {doc.filename}{jobs[index]?.status === "FAILED" ? " (failed)" : ""}
              </button>
            ))}
          </div>
          <Dock
            docId={documents[activeDoc].docId}
            disabled={jobs[activeDoc]?.status === "FAILED"}
            tabs={buildDockTabs(documents[activeDoc].docId, {
              findingCount: findingsForDocument(findingsFor(documents[activeDoc].caseId), documents[activeDoc].docId).length,
              disabledReason: jobs[activeDoc]?.status === "FAILED" ? "Analysis failed for this document" : undefined,
            })}
          />
        </>
      )}
    </>
  );
}

interface UploadStepProps {
  picked: Picked[]; errors: string[]; dragging: boolean; setDragging: (value: boolean) => void; addFiles: (files: FileList | File[]) => void;
  inputRef: React.RefObject<HTMLInputElement>; remove: (index: number) => void;
  cases: Array<{ caseId: string; title: string }>; casesLoading: boolean; caseId: string; setCaseId: (value: string) => void;
  creatingCase: boolean; setCreatingCase: (value: boolean) => void; newCaseTitle: string; setNewCaseTitle: (value: string) => void;
  createCase: () => void; createPending: boolean; createError: string;
  useSamples: () => void; loadingSamples: boolean;
  submit: () => void; uploading: boolean; progress: number | null;
}

function UploadStep(props: UploadStepProps) {
  const { picked, errors, dragging, setDragging, addFiles, inputRef } = props;
  return (
    <div className="upload-grid">
      <div>
        <div
          className={`drop-zone ${dragging ? "drop-zone-active" : ""}`}
          onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => { event.preventDefault(); setDragging(false); addFiles(event.dataTransfer.files); }}
        >
          <span className="drop-icon"><Icon name="upload" size={24} /></span>
          <strong>Drop documents here</strong>
          <span>PDF, PNG, JPG or TIFF · up to {maxUploadMegabytes} MB per file</span>
          <div className="drop-actions">
            <Button type="button" variant="secondary" onClick={() => inputRef.current?.click()}>Browse files</Button>
            <Button type="button" variant="quiet" onClick={props.useSamples} disabled={props.loadingSamples}>{props.loadingSamples ? "Loading samples…" : "Use sample documents"}</Button>
          </div>
          <input ref={inputRef} type="file" aria-label="Choose documents" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,application/pdf,image/png,image/jpeg,image/tiff" multiple hidden onChange={(event) => { if (event.target.files) addFiles(event.target.files); event.target.value = ""; }} />
        </div>
        {errors.length > 0 && <div className="validation-alert" role="alert"><strong>Some files need attention</strong>{errors.map((error) => <p key={error}>{error}</p>)}</div>}
        {picked.length > 0 && (
          <ul className="file-list" aria-label="Selected files">
            {picked.map((item, index) => (
              <li key={`${item.file.name}-${item.file.size}`} className="file-row">
                <span className="file-thumb"><Icon name="file" /></span>
                <span className="file-copy">
                  <strong>{item.file.name}</strong>
                  <small>{formatBytes(item.file.size)} · SHA-256 <span className="mono-value">{item.sha256 === "pending" ? "computing…" : item.sha256 ?? "unavailable in this browser context"}</span></small>
                </span>
                <button type="button" className="icon-button" aria-label={`Remove ${item.file.name}`} onClick={() => props.remove(index)}><Icon name="close" size={16} /></button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <aside className="upload-side">
        <div className="field-group">
          <div className="field-row">
            <label className="field-label" htmlFor="scan-case">Case</label>
            <Button type="button" variant="quiet" onClick={() => props.setCreatingCase(!props.creatingCase)}>{props.creatingCase ? "Cancel" : "New case"}</Button>
          </div>
          <select id="scan-case" className="text-input" value={props.caseId} onChange={(event) => props.setCaseId(event.target.value)} disabled={props.casesLoading || props.cases.length === 0}>
            {props.cases.map((item) => <option key={item.caseId} value={item.caseId}>{item.caseId} — {item.title}</option>)}
          </select>
          {props.creatingCase && (
            <form className="inline-form" onSubmit={(event) => { event.preventDefault(); if (props.newCaseTitle.trim()) props.createCase(); }}>
              <label className="field-label" htmlFor="scan-case-title">New case title</label>
              <input id="scan-case-title" className="text-input" value={props.newCaseTitle} onChange={(event) => props.setNewCaseTitle(event.target.value)} maxLength={200} required />
              {props.createError && <p className="form-error" role="alert">{props.createError}</p>}
              <Button type="submit" disabled={props.createPending || !props.newCaseTitle.trim()}>{props.createPending ? "Creating…" : "Create and select case"}</Button>
            </form>
          )}
          {props.cases.length === 0 && !props.casesLoading && !props.creatingCase && <p className="form-error">Create a case before uploading files.</p>}
        </div>
        {mockMode && (
          <div className="notice-card" role="note">
            <strong>Fixture mode</strong>
            <p>Only the recorded sample documents (D-01, D-02, D-03 and the other recorded D-numbers) are analysed, and they keep their recorded case. Any other file is accepted but receives no analysis.</p>
          </div>
        )}
        <Button disabled={picked.length === 0 || !props.caseId || props.uploading} onClick={props.submit}>
          {props.uploading ? "Sending…" : `Analyse ${picked.length || ""} ${picked.length === 1 ? "file" : "files"}`}
        </Button>
        {props.uploading && (
          <div className="upload-transfer" role="status" aria-live="polite">
            <div className="upload-transfer-track" role="progressbar" aria-label="File upload progress" aria-valuemin={0} aria-valuemax={100} {...(props.progress === null ? {} : { "aria-valuenow": props.progress })}>
              <span className={props.progress === null ? "upload-transfer-indeterminate" : ""} style={props.progress === null ? undefined : { width: `${props.progress}%` }} />
            </div>
            <span>{props.progress === null ? "Sending files…" : props.progress === 100 ? "Bytes sent; waiting for server…" : `Uploading · ${props.progress}%`}</span>
          </div>
        )}
      </aside>
    </div>
  );
}

function AnalyzeStep({ jobs, documents, active, setActive }: { jobs: Job[]; documents: DocumentRecord[]; active: number; setActive: (index: number) => void }) {
  const totals = jobs.reduce((sum, job) => { const count = settledCount(job); return { settled: sum.settled + count.settled, total: sum.total + count.total }; }, { settled: 0, total: 0 });
  const fraction = totals.total === 0 ? 0 : totals.settled / totals.total;
  const job = jobs[active];
  return (
    <div className="analyze-grid">
      <div className="analyze-ring">
        <Ring fraction={fraction} tone="red" size="lg" label={`Analysis progress: ${totals.settled} of ${totals.total} analyzer checks settled`}>
          <span className="ring-value">{Math.round(fraction * 100)}</span><span className="ring-verdict">Percent</span>
        </Ring>
        <p className="gauge-caption" aria-live="polite">{totals.settled} of {totals.total} analyzer checks settled across {jobs.length} {jobs.length === 1 ? "document" : "documents"}</p>
      </div>
      <div>
        <div className="doc-switcher" role="group" aria-label="Documents being analysed">
          {documents.map((doc, index) => (
            <button key={doc.docId} type="button" className="chip" aria-pressed={index === active} onClick={() => setActive(index)}>{doc.filename} · {jobs[index]?.status}</button>
          ))}
        </div>
        {job && (
          <div className="analyze-doc">
            <div className="analyze-doc-head"><strong>{documents[active]?.filename}</strong><StatusBadge status={job.status} /></div>
            {job.error && <p className="form-error" role="alert">Analysis failed: {job.error}</p>}
            <AnalyzerChecklist analyzers={job.analyzers} />
          </div>
        )}
      </div>
    </div>
  );
}

interface ResultStepProps {
  jobs: Job[]; documents: DocumentRecord[]; active: number; setActive: (index: number) => void; ready: boolean;
  summaryFor: (caseId: string) => Awaited<ReturnType<typeof api.getCase>> | undefined;
  findingsFor: (caseId: string) => Awaited<ReturnType<typeof api.findings>>["findings"];
  autoOpen: boolean; setAutoOpen: (value: boolean) => void; reset: () => void;
}

function ResultStep({ jobs, documents, active, ready, summaryFor, findingsFor, autoOpen, setAutoOpen, reset }: ResultStepProps) {
  const doc = documents[active];
  const job = jobs[active];
  if (!doc || !job) return null;
  const failed = job.status === "FAILED";
  const summary = summaryFor(doc.caseId);
  const priority = summary?.perDocumentPriority[doc.docId] ?? null;
  const findings = findingsForDocument(findingsFor(doc.caseId), doc.docId);
  const top = [...findings].sort((a, b) => severityRank(b.finding.severity) - severityRank(a.finding.severity) || b.finding.confidence - a.finding.confidence);
  const coverage = jobCoverage(job);
  const openable = !failed;
  return (
    <div className="result-grid">
      <div className="result-gauge">
        {failed ? (
          <Gauge score={null} caption="No result: analysis failed for this document." />
        ) : (
          <Gauge score={priority?.score ?? null} band={priority?.priorityBand} caption={!ready ? "Reasoning is still preparing a result…" : priority ? "Investigation priority for this document. A priority, not a probability." : "Reasoning has not produced a priority for this document yet."} />
        )}
      </div>
      <div className="result-body">
        <div className="analyze-doc-head"><h2>{doc.filename}</h2><StatusBadge status={job.status} /></div>
        <span className="mono-value">{doc.docId} · SHA-256 {doc.sha256}</span>
        {failed && <p className="form-error" role="alert">Analysis failed: {job.error ?? "The analyzers reported an error."} The other uploaded documents are unaffected.</p>}
        <dl className="readings" aria-label="Three separate readings">
          <div><dt className="label">Confidence</dt><dd className="big-number">{priority ? pct(priority.confidence) : "—"}</dd><small>how sure the evidence is</small></div>
          <div><dt className="label">Severity of top finding</dt><dd className="big-number reading-text">{top[0] ? humanize(top[0].finding.severity) : failed ? "—" : "None observed"}</dd><small>how much it would matter if confirmed</small></div>
          <div><dt className="label">Coverage</dt><dd className="big-number">{coverage.ran} / {coverage.possible}</dd><small>{priority?.incompleteCoverage ? `analyzers that ran of those reported; ${priority.missingAnalyzers?.length ?? 0} more expected but not installed (${(priority.missingAnalyzers ?? []).join(", ")})` : "possible analyzers that ran"}</small></div>
        </dl>
        {!failed && (
          <section>
            <p className="label">Primary observations</p>
            {top.length === 0 ? (
              <p className="muted-text">{coverage.ran > 0 ? (priority?.incompleteCoverage ? "No anomalies observed by the analyzers that ran. Coverage is incomplete, so this is not a clean result." : "No anomalies observed by the analyzers that ran.") : "No analyzer ran, so this is not a clean result."}</p>
            ) : (
              <ol className="why-list">
                {top.slice(0, 3).map((item, index) => (
                  <li key={item.finding.findingId}><span className="why-index">{String(index + 1).padStart(2, "0")}</span><span><strong>{humanize(item.finding.type)}</strong><small>{observationLabel(item.finding.observationStatus)} · {pct(item.finding.confidence)} confidence</small></span></li>
                ))}
              </ol>
            )}
          </section>
        )}
        <div className="result-actions">
          {openable ? <Link className="button button-primary" to={`/documents/${encodeURIComponent(doc.docId)}/overview`}>OPEN INVESTIGATION <Icon name="arrow" size={15} /></Link> : <span className="button button-primary dock-tab-disabled" role="link" aria-disabled="true" tabIndex={0}>OPEN INVESTIGATION</span>}
          <Button variant="secondary" onClick={reset}>Scan another document</Button>
          <label className="toggle-control"><input type="checkbox" checked={autoOpen} onChange={(event) => setAutoOpen(event.target.checked)} /> Open the investigation automatically when results are ready</label>
        </div>
        {jobs.length > 1 && <p className="muted-text"><Chip tone="grey">{jobs.filter((item) => item.status === "FAILED").length} failed</Chip> of {jobs.length} documents. Switch documents below.</p>}
      </div>
    </div>
  );
}
