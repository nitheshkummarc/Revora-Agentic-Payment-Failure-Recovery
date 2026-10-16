import { SOURCE_URL } from "./ui";

export function Footer() {
  return (
    <footer className="border-t border-line">
      <div className="mx-auto flex max-w-[1240px] flex-col gap-6 px-5 py-14 md:flex-row md:items-end md:justify-between md:px-8">
        <div>
          <p className="text-lg font-semibold tracking-[-0.02em]">Revora</p>
          <p className="mt-2 max-w-[52ch] text-sm text-muted">
            Built for Razorpay's AI Buildathon, AI Revenue Recovery track. A simulation on a mock gateway with
            synthetic payments, not a live product.
          </p>
        </div>
        <ul className="flex flex-wrap gap-x-6 gap-y-2 text-sm text-muted">
          <li>
            <a className="transition-colors hover:text-ink" href={SOURCE_URL} target="_blank" rel="noreferrer">
              Source
            </a>
          </li>
          <li>
            <a className="transition-colors hover:text-ink" href={`${SOURCE_URL}/blob/main/docs/architecture.md`} target="_blank" rel="noreferrer">
              Architecture
            </a>
          </li>
          <li>
            <a className="transition-colors hover:text-ink" href={`${SOURCE_URL}/blob/main/docs/methodology.md`} target="_blank" rel="noreferrer">
              Methodology
            </a>
          </li>
        </ul>
      </div>
    </footer>
  );
}
