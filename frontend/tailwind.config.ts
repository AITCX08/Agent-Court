import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        ink: {
          900: '#0b0d10',
          800: '#10141a',
          700: '#1a1f25',
          600: '#2a3239',
        },
        accent: {
          500: '#a5d4ff',
          400: '#c2e0ff',
        },
      },
      animation: {
        'pulse-soft': 'pulse 2.4s cubic-bezier(0.4, 0, 0.6, 1) infinite',
      },
    },
  },
  plugins: [],
};

export default config;
