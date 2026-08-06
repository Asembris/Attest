import { Component } from 'react';
import type { ReactNode } from 'react';

// AN ERROR BOUNDARY SCOPED TO THE HERO'S BACKDROP, AND THE SCOPE IS THE POINT. It wraps the
// lattice and nothing else, so the blast radius of a decorative canvas is the decoration. A
// boundary at the root would also stop the black page, and it would do it by catching product
// failures in the same net as a shader — turning a real bug in the audit path into a quiet
// fallback. This one can only ever swallow the background.
//
// IT MUST SIT OUTSIDE `Suspense`, not inside it. Suspense handles a PENDING promise; it does
// not catch a REJECTED one. A lazy chunk that fails to load rethrows during render, which is
// an error, which is this component's job — and measurably so: with no boundary the whole tree
// unmounted ~2s after the failed fetch, having rendered correctly first.
//
// It catches BOTH failure paths on purpose. `getDerivedStateFromError` covers the render-phase
// throw (the rejected lazy import); the same boundary covers the commit-phase throw from
// HeroLattice's effect (`THREE.WebGLRenderer: Error creating WebGL context.` — real, and the
// reason is usually `disabled by enterprise policy`, i.e. a managed laptop, not a broken one).
//
// There is deliberately no retry and no reset. Both failures are properties of the machine the
// page is open on, not of the moment: a browser with WebGL off will have it off on the next
// render too, and re-throwing on a loop would spend a reader's battery to reach the same
// fallback. It degrades once and stays degraded.
type Props = {
  children: ReactNode;
  // Passed in rather than imported, so this file has no opinion about what the hero looks
  // like — it only knows that something must be rendered instead.
  fallback: ReactNode;
};

type State = { failed: boolean };

export default class LatticeBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  // LOUD, NOT SILENT. The page recovers on its own and the reader is not asked to do anything,
  // but a background that quietly stopped existing is precisely the report this repo already
  // spent a session chasing across a deployment. One line, naming the cause, so the next person
  // who is told "the animation is missing" can read the answer instead of measuring for it.
  componentDidCatch(error: Error) {
    console.warn(
      '[attest] hero lattice disabled, falling back to the static field:',
      error?.message ?? error
    );
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}
