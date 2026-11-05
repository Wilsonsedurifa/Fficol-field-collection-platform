/** @type {import('tailwindcss').Config} */
module.exports = {
  content: {
    relative: true,
    files: [
      '../templates/**/*.html',
      '../static/js/**/*.js',
    ],
  },
  safelist: [
    'bg-indigo-600',
    'bg-indigo-50',
    'text-indigo-800',
    'text-indigo-900',
    'bg-indigo-100',
    'bg-emerald-50',
    'text-emerald-800',
    'bg-amber-50',
    'text-amber-800',
    'bg-rose-50',
    'text-rose-800',
    'border-rose-300',
    'text-white',
    'text-slate-700',
    'bg-white',
    'sm:grid-cols-2',
  ],
  theme: {
    extend: {},
  },
  plugins: [],
}
