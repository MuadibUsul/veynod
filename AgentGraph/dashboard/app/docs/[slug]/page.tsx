import type { Metadata } from "next";

import { DocsView } from "../docs-view";
import { docsMetadata, docsPageJsonLd } from "../seo";
import { site } from "../site";

export const dynamicParams = false;

export function generateStaticParams() {
  return Object.keys(site.pages)
    .filter((slug) => slug !== "introduction")
    .map((slug) => ({ slug }));
}

export function generateMetadata({ params }: { params: { slug: string } }): Metadata {
  return docsMetadata({ slug: params.slug, lang: "en" });
}

export default function DocsPage({ params }: { params: { slug: string } }) {
  const jsonLd = docsPageJsonLd({ slug: params.slug, lang: "en" });
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{
          __html: JSON.stringify(jsonLd).replace(/</g, "\\u003c"),
        }}
      />
      <DocsView slug={params.slug} lang="en" />
    </>
  );
}
