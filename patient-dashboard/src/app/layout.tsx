import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Patient Dashboard — OpenEMR",
  description:
    "Modern patient dashboard for OpenEMR. Live FHIR data, server-rendered, " +
    "OAuth2/OpenID Connect authenticated.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body
        // Browser extensions (e.g. CNET Shopping injects cnet-shopping-enabled,
        // Grammarly injects data-gr-*, ColorZilla injects cz-shortcut-listen)
        // mutate <body> attributes before React hydrates. That mismatch is
        // outside our control and harmless, so we suppress the warning here ONLY.
        // Do NOT spread suppressHydrationWarning to children — it would mask
        // real bugs in our own server/client output.
        suppressHydrationWarning
        className="min-h-full bg-zinc-50 font-sans text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100"
      >
        {children}
      </body>
    </html>
  );
}
