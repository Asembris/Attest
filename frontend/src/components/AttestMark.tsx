// The Attest mark: a triangular constellation reading as an "A" — apex node, two base
// nodes, a crossbar edge, a sealed centre. Geometry copied verbatim from the brand kit
// (assets/brand/attest-mark.svg and attest-mark-small.svg), normalised 100 x 96 space.
//
// INLINED RATHER THAN IMPORTED. The kit's own guidance is `import Mark from "...svg?react"`,
// which needs vite-plugin-svgr — a build-plugin dependency for twelve primitives. Inlining
// keeps `currentColor` working, which is the whole point: the mark inherits the surrounding
// text colour and can never be handed a hex.
//
// THE MARK IS MONOCHROME BY RULE, and it is a rule with teeth here specifically: the verdict
// colours (supported / contradicted / insufficient) belong to the CLAIM ROWS. This component
// takes no colour prop for that reason — tint it from the caller's `className` with an ink
// token, never with a verdict token. What it replaced was a green `ShieldCheck` in
// `text-supported`, i.e. the identity wearing a verdict.
export default function AttestMark({
  size = 18,
  className = '',
}: {
  size?: number;
  className?: string;
}) {
  // The kit's threshold, enforced HERE so no caller can get it wrong: below 32px the full
  // mark's 2.6-unit strokes, 3r crossbar joints and 10.5r centre ring turn to mush, so the
  // small cut (heavier stroke, joints and ring dropped) is what ships. Every placement in
  // this app is 18-20px, i.e. all of them take the small cut.
  const small = size < 32;
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 100 96"
      width={size}
      height={size}
      fill="none"
      // Every placement sits directly beside the literal word "Attest", so the mark is
      // decorative here: a second accessible name would double-announce, and the browser
      // E2E's `get_by_role("heading", name="Attest")` should not gain a sibling that also
      // matches on the word.
      aria-hidden="true"
      focusable="false"
      className={className}
    >
      <g
        fill="none"
        stroke="currentColor"
        strokeWidth={small ? 4.2 : 2.6}
        strokeLinecap="round"
      >
        <path d="M50 10 L10 86" />
        <path d="M50 10 L90 86" />
        <path d="M22.63 62 L77.37 62" />
        {!small && <circle cx="50" cy="62" r="10.5" strokeWidth="2.21" />}
      </g>
      <g fill="currentColor">
        <circle cx="50" cy="10" r={small ? 8.06 : 4.99} />
        <circle cx="10" cy="86" r={small ? 8.06 : 4.99} />
        <circle cx="90" cy="86" r={small ? 8.06 : 4.99} />
        {!small && <circle cx="22.63" cy="62" r="2.99" />}
        {!small && <circle cx="77.37" cy="62" r="2.99" />}
        <circle cx="50" cy="62" r={small ? 6.8 : 4.21} />
      </g>
    </svg>
  );
}
