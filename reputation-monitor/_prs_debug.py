import logging, sys, time, traceback
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Silence overly chatty libs
logging.getLogger("httpcore").setLevel(logging.INFO)
logging.getLogger("urllib3").setLevel(logging.INFO)

sys.path.insert(0, ".")
from app.scraper.prs_scraper import (
    find_podmiot, list_documents, download_document_raw,
    document_to_text, _client,
)

KRS = "0000028860"
with _client() as cli:
    t0 = time.perf_counter()
    p = find_podmiot(KRS, client=cli)
    print(f"podmiot ({time.perf_counter()-t0:.2f}s): {p}")

    t0 = time.perf_counter()
    docs = list_documents(KRS, client=cli)
    print(f"\nlist_documents ({time.perf_counter()-t0:.2f}s): {len(docs)} docs")
    for d in docs[:8]:
        print(f"  {d.rodzaj:>3} {d.rodzaj_label[:35]:35} {d.period_start}..{d.period_end} id={d.id[:16]}..")

    # Only try rodzaj=3 (Roczne sprawozdanie finansowe) first
    candidates = [d for d in docs if d.is_financial and d.period_end][:3]
    for d in candidates:
        print(f"\n=== Downloading {d.rodzaj} {d.rodzaj_label} {d.period_end} ===")
        t0 = time.perf_counter()
        try:
            blob = download_document_raw(KRS, d, client=cli)
        except Exception as e:
            print(f"  DL FAILED: {e}")
            continue
        print(f"  DL ok ({time.perf_counter()-t0:.2f}s, {len(blob)} bytes, head={blob[:40]!r})")
        t0 = time.perf_counter()
        try:
            text = document_to_text(blob)
        except Exception as e:
            print(f"  EXTRACT FAILED: {e}")
            traceback.print_exc()
            continue
        print(f"  EXTRACT ok ({time.perf_counter()-t0:.2f}s, {len(text)} chars)")
        print(f"  sample: {text[:300]}")
