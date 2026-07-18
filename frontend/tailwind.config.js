/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // Base surfaces — near-black, layered greys
        ink: {
          950: '#0A0B0D',
          900: '#101216',
          850: '#14161A',
          800: '#1A1D22',
          700: '#22262D',
          600: '#2C313A',
          500: '#3A4049',
          400: '#565D67',
          300: '#7B8390',
          200: '#A8AEB8',
          100: '#D2D6DC',
          50: '#EDEFF2',
        },
        // Verdict states — the muted design-pass palette. The MEANING is frozen: three
        // verdict tokens map 1:1 to the old ones, only the hue changed. Shade variants are
        // recomputed around the new DEFAULT (only DEFAULT + accent-400/600 are referenced in
        // the app today, but the scale is kept complete so a future utility resolves sanely).
        supported: {
          DEFAULT: '#5FB98D',
          600: '#4FA87B',
          400: '#7FC9A3',
          200: '#AEDCC2',
          glow: 'rgba(95, 185, 141, 0.12)',
        },
        contradicted: {
          DEFAULT: '#D96A64',
          600: '#C25852',
          400: '#E4867F',
          200: '#EFB3AE',
          glow: 'rgba(217, 106, 100, 0.12)',
        },
        insufficient: {
          DEFAULT: '#C2A265',
          600: '#A98A4F',
          400: '#D2B57F',
          200: '#E3D2A8',
          glow: 'rgba(194, 162, 101, 0.12)',
        },
        // Neutral accent
        accent: {
          DEFAULT: '#6E8FBF',
          400: '#88A5CD',
          600: '#5577A6',
        },
      },
      fontFamily: {
        serif: ['"Cormorant Garamond"', 'Georgia', 'serif'],
        sans: ['"Figtree"', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', '"SF Mono"', 'monospace'],
      },
      fontSize: {
        display: ['clamp(3rem, 8vw, 6.5rem)', { lineHeight: '0.95', letterSpacing: '-0.03em' }],
        headline: ['clamp(1.75rem, 4vw, 2.75rem)', { lineHeight: '1.05', letterSpacing: '-0.02em' }],
      },
      letterSpacing: {
        tightest: '-0.04em',
      },
      maxWidth: {
        prose: '68ch',
      },
      keyframes: {
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        'pulse-soft': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.5' },
        },
        'shimmer': {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
      },
      animation: {
        'fade-in': 'fade-in 0.6s ease-out forwards',
        'pulse-soft': 'pulse-soft 2.5s ease-in-out infinite',
        'shimmer': 'shimmer 2s linear infinite',
      },
    },
  },
  plugins: [],
};
