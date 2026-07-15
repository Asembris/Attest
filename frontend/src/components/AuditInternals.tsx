import { useState, type ReactNode } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  ChevronDown,
  Microscope,
  Route,
  ShieldCheck,
  ShieldAlert,
  Database,
  AlertTriangle,
  Sparkles,
  FileText,
  Ban,
  GitCompare,
} from 'lucide-react';
import type { AuditRecord, ClaimRecord } from '../api/types';

// Phase 4b: the audit-internals surface. Everything here already exists in the AuditRecord
// and had no UI home — and it is what makes Attest an AUDITOR rather than a scorer. It is
// reachable but deliberately not front-and-center: a collapsible drawer at the foot of the
// results, closed by default.
//
// Four areas, each sourced verbatim from the record:
//   - Trajectory      the run held to its own architecture; NO_LLM_IN_THE_VERDICT_PATH by name
//   - Guard-fire      per claim: was the explanation faithful, model-authored, any drafts rejected
//   - Cache receipt   catalog lookups -> fetches (the consistency-and-load story)
//   - Accountability  dropped claims, injection findings, model/checker conflicts
//
// Nothing is computed or embellished; the numbers are the ones the run measured.

const LLM_RULE = 'no-llm-in-the-verdict-path';

/** Prettify a rule slug: "no-llm-in-the-verdict-path" -> "no llm in the verdict path". */
function ruleLabel(slug: string): string {
  return slug.replace(/-/g, ' ');
}

function SectionTitle({
  icon: Icon,
  title,
  accent = 'text-ink-200',
}: {
  icon: typeof Route;
  title: string;
  accent?: string;
}) {
  return (
    <div className="flex items-center gap-2 mb-3">
      <Icon size={14} className={accent} />
      <h4 className={`text-sm font-medium ${accent}`}>{title}</h4>
    </div>
  );
}

function Trajectory({ record }: { record: AuditRecord }) {
  const r = record.receipts;
  const rules = r.rules_checked;
  const llmChecked = rules.includes(LLM_RULE);

  return (
    <div className="surface-raised p-5">
      <SectionTitle icon={Route} title="Trajectory" accent={r.trajectory_ok ? 'text-supported' : 'text-contradicted'} />

      {/* The headline invariant, called out by name. */}
      <div
        className={`flex items-start gap-3 p-3 rounded-lg mb-4 border ${
          llmChecked && r.trajectory_ok
            ? 'bg-supported/8 border-supported/25'
            : 'bg-ink-800/60 border-ink-600/40'
        }`}
      >
        <div className="flex items-center justify-center w-7 h-7 rounded-full bg-supported/15 text-supported shrink-0 mt-0.5">
          <ShieldCheck size={15} strokeWidth={2.5} />
        </div>
        <div className="min-w-0">
          <div className="font-mono-nums text-sm text-supported">no-llm-in-the-verdict-path</div>
          <p className="text-xs text-ink-300 mt-0.5 leading-relaxed">
            {llmChecked
              ? 'Asserted against this run: no model call spent a token between resolving the entity and the deterministic checker. The verdict was decided by code.'
              : 'Not exercised on this run.'}
          </p>
        </div>
      </div>

      <div className="flex items-center gap-2 mb-3 text-xs">
        <span
          className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md font-medium ${
            r.trajectory_ok
              ? 'bg-supported/12 text-supported'
              : 'bg-contradicted/12 text-contradicted'
          }`}
        >
          {r.trajectory_ok ? <ShieldCheck size={12} /> : <ShieldAlert size={12} />}
          {r.trajectory_ok ? 'Trajectory verified' : 'Trajectory violated'}
        </span>
        <span className="text-ink-400">{r.trajectory_summary}</span>
      </div>

      <div className="text-label-sm mb-2">Rules exercised ({rules.length})</div>
      <div className="flex flex-wrap gap-1.5">
        {rules.map((rule) => {
          const isLlm = rule === LLM_RULE;
          return (
            <span
              key={rule}
              className={`font-mono-nums text-[0.6875rem] px-2 py-0.5 rounded border ${
                isLlm
                  ? 'text-supported bg-supported/10 border-supported/30'
                  : 'text-ink-300 bg-ink-700/40 border-ink-600/40'
              }`}
            >
              {ruleLabel(rule)}
            </span>
          );
        })}
        {rules.length === 0 && <span className="text-xs text-ink-400">none recorded</span>}
      </div>
    </div>
  );
}

function GuardFire({ claims }: { claims: ClaimRecord[] }) {
  const authored = claims.filter((c) => c.explanation_source === 'model').length;
  const fellBack = claims.length - authored;
  const rejectedDrafts = claims.reduce((n, c) => n + c.rejected.length, 0);
  const unfaithful = claims.filter((c) => !c.faithful);
  // The interesting rows: anything the guard actually acted on.
  const flagged = claims.filter(
    (c) => c.rejected.length > 0 || c.faithfulness_violations.length > 0 || !c.faithful,
  );

  return (
    <div className="surface-raised p-5">
      <SectionTitle icon={ShieldCheck} title="Faithfulness guard" />

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
        <Stat label="Explanations" value={String(claims.length)} />
        <Stat label="Model-authored" value={String(authored)} icon={<Sparkles size={11} />} />
        <Stat label="Safe template" value={String(fellBack)} icon={<FileText size={11} />} tone={fellBack > 0 ? 'warn' : 'ok'} />
        <Stat label="Drafts rejected" value={String(rejectedDrafts)} tone={rejectedDrafts > 0 ? 'warn' : 'ok'} />
      </div>

      {flagged.length === 0 ? (
        <p className="text-xs text-ink-400 leading-relaxed">
          Every explanation was checked token-by-token against the cited evidence and shipped
          as written — no draft was rejected, and nothing fell back to a template.
        </p>
      ) : (
        <div className="space-y-2">
          <div className="text-label-sm">Guard acted on</div>
          {flagged.map((c) => (
            <div key={c.index} className="text-xs bg-ink-800/50 border border-ink-600/40 rounded-lg p-3">
              <div className="flex items-center gap-2 mb-1">
                <span className="font-mono-nums text-ink-400">#{String(c.index + 1).padStart(2, '0')}</span>
                <span className="text-ink-200">{c.claim_type}</span>
                <span className={`ml-auto inline-flex items-center gap-1 ${c.faithful ? 'text-supported' : 'text-contradicted'}`}>
                  {c.faithful ? <ShieldCheck size={11} /> : <ShieldAlert size={11} />}
                  {c.faithful ? 'faithful' : 'unfaithful'}
                </span>
              </div>
              {c.faithfulness_violations.length > 0 && (
                <div className="text-ink-300 mt-1">
                  Unevidenced tokens:{' '}
                  {c.faithfulness_violations.map((v, i) => (
                    <span key={i} className="font-mono-nums text-contradicted">
                      {v.token}
                      {i < c.faithfulness_violations.length - 1 ? ', ' : ''}
                    </span>
                  ))}
                </div>
              )}
              {c.rejected.map((d, i) => (
                <div key={i} className="text-ink-400 mt-1 pl-2 border-l border-ink-600/50 leading-relaxed">
                  rejected draft: {d}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
      {unfaithful.length > 0 && (
        <p className="mt-3 text-xs text-contradicted">
          {unfaithful.length} explanation{unfaithful.length === 1 ? '' : 's'} shipped the
          deterministic template because the model's prose could not be entailed by the evidence.
        </p>
      )}
    </div>
  );
}

function CacheReceipt({ record }: { record: AuditRecord }) {
  const r = record.receipts;
  const saved = r.catalog_lookups - r.catalog_fetches;

  return (
    <div className="surface-raised p-5">
      <SectionTitle icon={Database} title="Catalog receipt" />
      <div className="flex items-center gap-2 font-mono-nums text-sm text-ink-100 mb-3">
        <span>{r.catalog_lookups}</span>
        <span className="text-label-sm">lookups</span>
        <span className="text-ink-500">over</span>
        <span>{r.catalog_entities}</span>
        <span className="text-label-sm">datasets</span>
        <span className="text-ink-500">→</span>
        <span className="text-supported">{r.catalog_fetches}</span>
        <span className="text-label-sm">fetches</span>
      </div>
      <p className="text-xs text-ink-400 leading-relaxed">
        {saved > 0 ? (
          <>
            {saved} catalog read{saved === 1 ? '' : 's'} served from the run's single snapshot.
            Every claim about one dataset was decided against one read of it — a consistency
            boundary, not just a saving: the run cannot return two answers about a catalog that
            changed mid-audit.
          </>
        ) : (
          <>
            Each claim named a distinct dataset, so every lookup was a real fetch. The cache is a
            consistency boundary first: a repeat lookup within this run would be served from the
            one snapshot it already holds.
          </>
        )}
      </p>
    </div>
  );
}

function Accountability({ record }: { record: AuditRecord }) {
  const conflicts = record.claims.flatMap((c) =>
    c.conflicts.map((k) => ({ index: c.index, ...k })),
  );
  const nothing =
    record.dropped.length === 0 &&
    record.injection_findings.length === 0 &&
    conflicts.length === 0 &&
    record.errors.length === 0;

  return (
    <div className="surface-raised p-5">
      <SectionTitle icon={AlertTriangle} title="Accountability" />

      {nothing && (
        <p className="text-xs text-ink-400 leading-relaxed">
          Nothing was dropped, no instruction-like span was stripped from the agent's text, the
          model and the deterministic core never disagreed, and every claim named a real entity.
          A clean run says so explicitly.
        </p>
      )}

      {record.errors.length > 0 && (
        <Row
          icon={<Ban size={12} className="text-insufficient" />}
          title={`${record.errors.length} claim${record.errors.length === 1 ? '' : 's'} could not be checked`}
          note="The entity does not exist — a malformed question, not a verdict. Shown in full above."
        />
      )}

      {record.dropped.length > 0 && (
        <div className="mb-3">
          <Row
            icon={<Ban size={12} className="text-contradicted" />}
            title={`${record.dropped.length} claim${record.dropped.length === 1 ? '' : 's'} dropped by the decomposer`}
            note="Refused before any check — a minted URN or a claim that failed validation."
          />
          {record.dropped.map((d, i) => (
            <div key={i} className="text-xs text-ink-400 mt-1 pl-6 font-mono-nums break-words">
              {d.reason}
            </div>
          ))}
        </div>
      )}

      {record.injection_findings.length > 0 && (
        <div className="mb-3">
          <Row
            icon={<ShieldAlert size={12} className="text-contradicted" />}
            title={`${record.injection_findings.length} instruction-like span${record.injection_findings.length === 1 ? '' : 's'} stripped`}
            note="Redacted from the agent's output before any model saw it — an attempt to talk out of the audit is itself a finding."
          />
          {record.injection_findings.map((f, i) => (
            <div key={i} className="text-xs text-ink-400 mt-1 pl-6 font-mono-nums break-words">
              {f.pattern}: “{f.matched}”
            </div>
          ))}
        </div>
      )}

      {conflicts.length > 0 && (
        <div>
          <Row
            icon={<GitCompare size={12} className="text-insufficient" />}
            title={`${conflicts.length} model/checker conflict${conflicts.length === 1 ? '' : 's'}`}
            note="The model read a verdict or a field differently from the deterministic core. Surfaced, never resolved — the verdict never moves."
          />
          {conflicts.map((k, i) => (
            <div key={i} className="text-xs text-ink-400 mt-1 pl-6 break-words">
              <span className="font-mono-nums text-ink-300">#{String(k.index + 1).padStart(2, '0')} {k.kind}</span> — {k.detail}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Row({ icon, title, note }: { icon: ReactNode; title: string; note: string }) {
  return (
    <div className="flex items-start gap-2">
      <span className="mt-0.5 shrink-0">{icon}</span>
      <div>
        <div className="text-sm text-ink-100">{title}</div>
        <div className="text-xs text-ink-400 leading-relaxed">{note}</div>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  icon,
  tone = 'ok',
}: {
  label: string;
  value: string;
  icon?: ReactNode;
  tone?: 'ok' | 'warn';
}) {
  return (
    <div className="rounded-lg bg-ink-800/50 border border-ink-600/40 px-3 py-2">
      <div className="text-label-sm flex items-center gap-1">
        {icon}
        {label}
      </div>
      <div className={`font-mono-nums text-sm font-medium ${tone === 'warn' ? 'text-insufficient' : 'text-ink-100'}`}>
        {value}
      </div>
    </div>
  );
}

export default function AuditInternals({ record }: { record: AuditRecord }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="surface-card overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-ink-800/40 transition-colors"
      >
        <Microscope size={16} className="text-ink-300 shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-ink-100">Audit internals</div>
          <div className="text-xs text-ink-400">
            The evidence trail: trajectory, the guard, the catalog receipt, and what was refused.
          </div>
        </div>
        <motion.div animate={{ rotate: open ? 180 : 0 }} className="text-ink-400 shrink-0">
          <ChevronDown size={18} />
        </motion.div>
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
            className="overflow-hidden"
          >
            <div className="px-5 pb-5 space-y-4">
              <div className="divider" />
              <Trajectory record={record} />
              <GuardFire claims={record.claims} />
              <CacheReceipt record={record} />
              <Accountability record={record} />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
