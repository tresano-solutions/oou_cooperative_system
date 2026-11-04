# CoopMS search visibility

## Implemented

- Public homepage title and description target cooperative management software
  in Nigeria; visible supporting copy reflects that subject naturally.
- Homepage, app guide and privacy page have a single self-referencing canonical
  on `https://www.cooperativems.com`. The sitemap uses the same URLs.
- Sitemap omits private applications and sign-in. Stale last-modified dates and
  unsupported priority assumptions were removed rather than inventing dates.
- Homepage includes Organization, WebSite and SoftwareApplication JSON-LD.
  No fabricated ratings, customers, certifications or pricing are included.
  This does not guarantee a rich search result.
- Homepage and app guide include share-preview metadata using an existing image.
- All Flask application responses carry `X-Robots-Tag: noindex, nofollow`.
  The static public website remains indexable. Sign-in has its own noindex meta.
- Public installation instructions no longer expose real client code examples.

Canonical tags express a preference; apex and www remain accessible. Do not add
a host redirect blindly: public form POSTs and campaign integrations must remain
compatible. Search exclusion is not access control; tenant authentication remains
required. Robots.txt deliberately does not block sign-in, so crawlers can see
its noindex instruction.

## Owner steps after deployment

1. Add a Domain property for `cooperativems.com` in Google Search Console.
2. Add the exact Google-provided verification TXT record in the authoritative
   DNS provider (Cloudflare if its nameservers are active), then verify.
3. Submit `https://www.cooperativems.com/sitemap.xml`.
4. Inspect the homepage and `/app` URLs and request indexing once.
5. Check Page indexing and Performance weekly: impressions, clicks, queries,
   countries and demo conversions. Search Console access is required for this;
   it cannot be inferred from site code.
6. Validate structured data with Google's Rich Results Test, and measure mobile
   performance with PageSpeed Insights. Large hero PNGs are a remaining image
   optimization opportunity; no performance score improvement is claimed yet.

## Content and app-store work still needed

Publish substantial, evidence-backed guides for cooperative savings records,
salary-deduction loans and financial reporting, with links to a demo. Use only
approved case studies, not invented testimonials. Avoid duplicate keyword pages.
Review the privacy policy's existing registered-address placeholder with the
business owner before treating it as final legal content.

Native app store optimization requires actual Play Store/App Store listings:
confirmed availability, screenshots, descriptions and developer account access.
The public `/app` page improves web discoverability but does not publish an app
or claim an iOS native release.

References:
- https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls
- https://developers.google.com/search/docs/crawling-indexing/block-indexing
