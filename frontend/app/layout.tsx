import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "TransitPay AI — CFO Control Dashboard",
  description:
    "Autonomous Freight & Demurrage Settlement Agent powered by XRPL + RLUSD. " +
    "Built for the Ripple Agentic Finance Hackathon.",
  keywords: ["XRPL", "RLUSD", "Freight", "Demurrage", "AI Agent", "TransitPay"],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className={inter.className}>{children}</body>
    </html>
  );
}
