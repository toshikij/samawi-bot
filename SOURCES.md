# Sources and licences

Every fact the bot states comes from one of these. Posts carry only the short inline reference
after a quote — *(QS. Al-Baqarah: 282)*, *(HR. Bukhari)* — except where a licence requires more,
as recorded below. The audit trail from each published claim to its source is the `sources` table.

| Content | Source | Licence / terms | On-post credit |
|---|---|---|---|
| Quran Arabic | Tanzil Uthmani (`quran-uthmani.txt`, bundled in the app) | Verbatim copying allowed; no changes | None on posts (inline QS reference only) |
| Quran translation | Terjemahan Kemenag (`translation-id-kemenag.json`) | Owner decision 2026-11-25: use publicly, no label. `TRANSLATION_MODE=makna` switches every future post to Arabic + reference + paraphrase | None |
| Duas | Samawi `duas.json` (only `needsReview: false`) | Samawi's own translations | Inline HR reference |
| Prayer times, astronomy | Kemenag/MABIMS engine, ported from the app and checked minute-for-minute against it | Computed | None |
| Hijri dates | Umm al-Qura (same as the app's `hijri` package) | Computed; Ramadan/Syawal starts always "perkiraan, menunggu sidang isbat" | None |
| Hadith (displayed) | HadeethEnc.com, Indonesian | See below | **Caption credit required** |
| Hadith grades (verification only) | fawazahmed0/hadith-api | Unlicense | Never displayed |

## HadeethEnc terms (read 2026-11-25)

The terms are not on a standalone page; they are the "Terms and Policies" dialog on
https://hadeethenc.com/en/home, headed *"Contents of the translations can be downloaded and
republished under the following terms"*. Summarised:

1. No modification, addition or deletion of the content.
2. **Clearly refer to the publisher and source (HadeethEnc.com).**
3. Mention the version number when republishing the translation.
4. Keep the translation version information inside the document.
5. Notify HadeethEnc of any note on the translation.
6. Update translations to the latest version they issue.
7. No inappropriate advertisements alongside the content.

Nothing restricts commercial use. The API (https://hadeethenc.com/api/v1, documented at a Postman
page with no terms) has no separate conditions.

**What the bot does as a result**

- The Indonesian translation is shown verbatim (rule 1; the renderer asserts byte equality).
- Any post that quotes a HadeethEnc hadith ends its caption with
  `Terjemahan hadis: HadeethEnc.com` (rule 2). The slide itself carries only *(HR. …)*, as the spec's
  §6.2 fallback allows: "If they require credit, add it to the caption only."
- The weekly `plan` job re-fetches any cached hadith older than 30 days, so the text tracks their
  latest version (rule 6).
- Rules 3–4 (version number): the API does not expose a version per hadith. This is one of the
  questions in the permission request below.

**Permission request — drafted, not sent.** The spec asks for a short request through
https://hadeethenc.com/en/home/contact (fields: name, email, message). Draft:

> Assalamu'alaikum. We run @samawi.indonesia, the Instagram and Threads account of Samawi, an
> ad-free Indonesian prayer and Quran app. We would like to quote HadeethEnc's Indonesian hadith
> translations verbatim in our posts, with the short reference (e.g. "HR. Bukhari") on the image
> and "Terjemahan hadis: HadeethEnc.com" in the caption. Two questions: (1) is a caption credit,
> rather than a credit on the image itself, acceptable? (2) The API does not show a version
> number per translation; how would you like us to cite the version? We only use hadith you grade
> sahih or hasan, never alter the text, and never show it next to advertisements.
> Jazakumullahu khairan.

Record their answer here when it arrives.
