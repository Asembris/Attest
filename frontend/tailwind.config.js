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
        // Verdict states
        supported: {
          DEFAULT: '#3FB984',
          600: '#34A372',
          400: '#5FC99A',
          200: '#9BDEC0',
          glow: 'rgba(63, 185, 132, 0.12)',
        },
        contradicted: {
          DEFAULT: '#E5484D',
          600: '#C73D41',
          400: '#ED6E72',
          200: '#F2A4A6',
          glow: 'rgba(229, 72, 77, 0.12)',
        },
        insufficient: {
          DEFAULT: '#C99A2E',
          600: '#AE8628',
          400: '#D6B256',
          200: '#E8D29A',
          glow: 'rgba(201, 154, 46, 0.12)',
        },
        // Neutral accent
        accent: {
          DEFAULT: '#6E8FBF',
          400: '#88A5CD',
          600: '#5577A6',
        },
      },
      fontFamily: {
        serif: ['"Fraunces"', 'Georgia', 'serif'],
        sans: ['"Inter"', 'system-ui', 'sans-serif'],
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
