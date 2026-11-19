/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        warm: {
          50: '#FAF8F5',
          100: '#F6F2EB',
          200: '#EFE8DE',
          300: '#E2D9CC',
          400: '#C4B7A4',
          500: '#9E917F',
          600: '#7E7469',
          700: '#5E554B',
          800: '#3D362F',
          900: '#231F1C',
        },
        brand: {
          orange: '#DE6438',
          orangeHover: '#C8542A',
          green: '#4D835C',
          blue: '#3E6B89',
          red: '#C24D4D',
        }
      },
      borderRadius: {
        'tactile': '14px',
        'arena': '36px',
      }
    },
  },
  plugins: [],
}
