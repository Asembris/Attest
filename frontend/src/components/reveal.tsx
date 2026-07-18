import { useEffect, useRef, useState, type ReactNode } from 'react';
import { motion, useInView, useReducedMotion } from 'framer-motion';

// Shared presentation primitives for the design-pass pages: a scroll-reveal wrapper and a
// count-up number. Both are pure animation — they hold no product data and assert nothing.
// The DC-runtime mockups did these with a hand-rolled IntersectionObserver + a RAF counter;
// here they are Framer's `useInView` and one small effect, and both honour
// prefers-reduced-motion (reveal skips the translate, count-up jumps straight to the value).

export function Reveal({
  children,
  delay = 0,
  y = 24,
  x = 0,
  className,
}: {
  children: ReactNode;
  delay?: number;
  y?: number;
  x?: number;
  className?: string;
}) {
  const reduced = useReducedMotion();
  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y: reduced ? 0 : y, x: reduced ? 0 : x }}
      whileInView={{ opacity: 1, y: 0, x: 0 }}
      viewport={{ once: true, margin: '-8% 0px -6% 0px' }}
      transition={{ duration: 0.7, delay, ease: [0.22, 1, 0.36, 1] }}
    >
      {children}
    </motion.div>
  );
}

/** A number that counts up from 0 to `value` when it first scrolls into view. `value` is
 *  supplied by the caller (a committed receipt) — this only animates the reveal of it. */
export function CountUp({
  value,
  suffix = '',
  decimals = 0,
  durationMs = 900,
  className,
}: {
  value: number;
  suffix?: string;
  decimals?: number;
  durationMs?: number;
  className?: string;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true, margin: '-8%' });
  const reduced = useReducedMotion();
  const [display, setDisplay] = useState(0);

  useEffect(() => {
    if (!inView) return;
    if (reduced) {
      setDisplay(value);
      return;
    }
    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / durationMs);
      const eased = 1 - Math.pow(1 - p, 3);
      setDisplay(value * eased);
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [inView, value, reduced, durationMs]);

  return (
    <span ref={ref} className={className}>
      {display.toFixed(decimals)}
      {suffix}
    </span>
  );
}
