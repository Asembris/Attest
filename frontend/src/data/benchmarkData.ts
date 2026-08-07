// The benchmark page's numbers. Every figure here traces to a committed receipt:
//   benchmark/results/core.json, full.json   — the metrics, confusion matrix, pass@k
//   benchmark/results/calibration*.json       — the two committed cross-family runs
//
// Do not "improve" these by hand. tests/test_calibration_consistency.py asserts every
// calibration row is arithmetically consistent and matches a committed receipt, and the
// benchmark's own suite (run_eval.py, plus the vacuity check that proves a broken checker
// moves the number — benchmark/README.md) holds the rest. A figure edited here to look better
// is a figure that no longer matches what the code measures. Everything the fabricated mock
// once claimed — 300 claims, 3 engineers, a GPT-4 "AI judge" baseline, temperature 0.7 — is
// gone, because none of it was ever measured.

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
  // full-pipeline receipts (full.json): the real model, 40 claims. Scorer v2.
  costUsd: 0.0153102,
  explanations: 40,
  modelAuthored: 39,
  guardRejected: 5,
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
// family) re-labels the 40 cases from the same declared policy. TWO runs are committed, both
// 39/40 (97.5%); every value here traces to a committed receipt:
//   benchmark/results/calibration-before-policy-declared.json  — disputed class-15
//   benchmark/results/calibration.json (post-policy)           — disputed class-03
// The disputed case differs between runs: declaring the governance tie-break resolved class-15
// (signal), and an unrelated prompt change flipped Nemotron's verdict on class-03 (noise). A
// judge that answers its own question differently at temperature 0 cannot BE ground truth —
// only calibrate it, which is why Attest's verdicts come from code. (benchmark/README.md)
export const calibrationMeta = {
  labeler: 'nvidia/llama-3.3-nemotron-super-49b-v1.5',
  family: 'Llama (NVIDIA Nemotron)',
  temperature: 0,
  runs: 2,
};

export interface CalibrationRun {
  condition: string;
  agreement: number;
  agreed: number;
  nCases: number;
  disputed: string[];
  nature: 'signal' | 'noise';
}

export const calibrationRuns: CalibrationRun[] = [
  { condition: 'before policy tie-break declared', agreement: 0.975, agreed: 39, nCases: 40, disputed: ['class-15'], nature: 'signal' },
  { condition: 'after policy tie-break declared', agreement: 0.975, agreed: 39, nCases: 40, disputed: ['class-03'], nature: 'noise' },
];
