export function BrandMark({ className }: { className?: string }) {
  return (
    <span className={className ? `brand-mark ${className}` : 'brand-mark'} aria-hidden="true">
      <svg viewBox="14 14 92 92" fill="currentColor" focusable="false">
        <path d="M 99.05 42 A 43 43 0 0 0 60 17 A 43 43 0 0 0 17 60 A 43 43 0 0 0 19.70 75 H 41.27 A 24 24 0 0 1 36 60 A 24 24 0 0 1 60 36 A 24 24 0 0 1 75.87 42 Z" />
        <path d="M 58 55 H 102.71 A 43 43 0 0 1 100.30 75 H 78.73 A 24 24 0 0 0 82.96 67 H 58 Z" />
        <path d="M 21.83 79.8 A 43 43 0 0 0 27.37 88 H 92.63 A 43 43 0 0 0 98.17 79.8 H 73.56 A 24 24 0 0 1 46.44 79.8 Z" />
        <path d="M 33.17 93.6 A 43 43 0 0 0 86.83 93.6 Z" />
      </svg>
    </span>
  );
}
