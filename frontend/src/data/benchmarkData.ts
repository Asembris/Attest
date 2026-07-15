// The benchmark page's numbers, sourced VERBATIM from the committed receipts:
//   benchmark/results/core.json, full.json           — the metrics and confusion matrix
//   benchmark/results/calibration*.json + README.md   — the 6-run cross-family calibration
//
// Do not "improve" these by hand. The benchmark's own suite holds them (run_eval.py, and the
// vacuity check that proves a broken checker moves the number — benchmark/README.md), so a
// figure edited here to look better is a figure that no longer matches what the code measures.
// Everything the fabricated mock claimed — 300 claims, 3 engineers, a GPT-4 "AI judge"
// accuracy baseline, temperature 0.7 — is gone, because none of it was ever measured.

export const benchmarkMeta = {
  nCases: 40,
  hard: 26,
  cells: 12, // 4 claim types x 3 verdicts, all populated
};

export interface VerdictMetric {
  verdict: 'Supported' | 'Contradicted' | 'Insufficient-Coverage';
  precision: number;
  recall: number;
  f1: number;
  support: number;
}

// core.json / full.json — identical, because the core and the full pipeline agree.
export const perVerdict: VerdictMetric[] = [
  { verdict: 'Supported', precision: 1, recall: 1, f1: 1, support: 15 },
  { verdict: 'Contradicted', precision: 1, recall: 1, f1: 1, support: 17 },
  { verdict: 'Insufficient-Coverage', precision: 1, recall: 1, f1: 1, support: 8 },
];

export const headline = {
  accuracy: 1.0,
  macroF1: 1.0,
  // pass@k is a bug DETECTOR, not a score: a deterministic checker cannot return two answers.
  passAtKCore: { k: 5, value: 1.0 },
  passAtKFull: { k: 3, value: 1.0 },
  // full-pipeline receipts (full.json): the real model, 40 claims.
  costUsd: 0.0138309,
  explanations: 40,
  modelAuthored: 40,
  guardRejected: 0,
};

// Rows = actual, columns = predicted [Supported, Contradicted, Insufficient-Coverage].
// A perfect diagonal over 40 claims. (The engine also has a fourth 'No-Claim' label for a
// mis-extraction; it was never used — every column of that row is 0 — so it is omitted here.)
export const confusion: { actual: string; predicted: number[] }[] = [
  { actual: 'Supported', predicted: [15, 0, 0] },
  { actual: 'Contradicted', predicted: [0, 17, 0] },
  { actual: 'Insufficient-Coverage', predicted: [0, 0, 8] },
];

// The cross-family calibration. Nemotron (Llama family — deliberately NOT the pipeline's GPT
// family) re-labels the 40 cases from the same declared policy. Six runs, identical code and
// prompt, temperature=0. The dispute set RESHUFFLES every run and no disagreement survives
// repetition — which is the sharpest possible argument for the deterministic core: a judge
// that answers its own question differently at temperature 0 cannot BE ground truth, only
// calibrate it. (benchmark/README.md)
export const calibrationMeta = {
  labeler: 'nvidia/llama-3.3-nemotron-super-49b-v1.5',
  family: 'Llama (NVIDIA Nemotron)',
  temperature: 0,
  runs: 6,
};

export interface CalibrationRun {
  run: number;
  agreement: number;
  disputed: string[];
}

export const calibrationRuns: CalibrationRun[] = [
  { run: 1, agreement: 0.975, disputed: ['class-15'] },
  { run: 2, agreement: 0.95, disputed: ['class-03', 'class-15'] },
  { run: 3, agreement: 0.95, disputed: ['class-03', 'class-13'] },
  { run: 4, agreement: 0.975, disputed: ['class-03'] },
  { run: 5, agreement: 0.975, disputed: [] },
  { run: 6, agreement: 0.95, disputed: [] },
];
