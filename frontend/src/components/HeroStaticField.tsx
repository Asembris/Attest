// THE HERO BACKDROP WHEN THE LATTICE CANNOT RUN. One visual, three triggers:
//
//   1. the reader asked for reduced motion  (Hero never mounts the lattice — correct, kept)
//   2. WebGL is unavailable                 (THREE.WebGLRenderer throws in the effect)
//   3. the lazy chunk never arrived         (blocked, proxied, or dropped)
//
// All three used to end in the same place and it was not this: 2 and 3 threw past a Suspense
// boundary that does not catch errors, with no error boundary anywhere above them, so the
// WHOLE app unmounted to a black page — measured `rootChildren: 0`, `bodyTextLen: 0`. 1 left
// the hero flat. LatticeBoundary catches 2 and 3; this component is what all three degrade to.
//
// IT IS PURE CSS, AND THAT IS A REQUIREMENT RATHER THAN A PREFERENCE. One of the failures it
// covers is "the network did not deliver a file", so a fallback that itself needs a request is
// the one thing it must not be — it would be most likely to be missing exactly when it is most
// needed. No image, no font, no second chunk: it is painted from the stylesheet that is already
// parsed by the time anything here can fail.
//
// IT DOES NOT ANIMATE, for the same reason. Trigger 1 is a reader who asked for no motion, and
// a fallback with a transition in it would answer that request by ignoring it.
//
// The palette is the lattice's own — `accent` (#6E8FBF) and the violet its nodes lerp toward
// (#9a8ff0), the two colours in HeroLattice.tsx — so the static hero reads as the same design
// at rest rather than as a different one. The pools sit where the field's brightest clusters
// tend to, asymmetric on purpose: an even wash reads as a gradient someone forgot to finish.
const FIELD = [
  'radial-gradient(38% 46% at 22% 30%, rgba(110,143,191,0.20) 0%, rgba(110,143,191,0) 68%)',
  'radial-gradient(30% 38% at 78% 22%, rgba(154,143,240,0.16) 0%, rgba(154,143,240,0) 70%)',
  'radial-gradient(46% 40% at 62% 78%, rgba(110,143,191,0.13) 0%, rgba(110,143,191,0) 72%)',
  'radial-gradient(28% 30% at 8% 82%, rgba(154,143,240,0.10) 0%, rgba(154,143,240,0) 70%)',
].join(', ');

export default function HeroStaticField() {
  // Decorative only — it renders nothing about the catalog, exactly as the lattice it stands
  // in for renders nothing about the catalog. Hidden from assistive tech rather than described.
  return <div aria-hidden="true" className="absolute inset-0" style={{ background: FIELD }} />;
}
