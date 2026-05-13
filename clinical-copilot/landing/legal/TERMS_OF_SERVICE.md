# Terms of Service — Clinical Co-Pilot

**Effective date:** 2026-05-12. Last updated: 2026-05-12.

**IMPORTANT — DRAFT TEMPLATE.** Have these reviewed by counsel before publishing to a paying customer. The redline budget overlap with the BAA review is ~$500-1,500.

These Terms of Service ("Terms") govern your use of the Clinical Co-Pilot service ("Service") operated by Scott Lydon ("we", "us"). By installing the OpenEMR module, signing the order form, or otherwise accessing the Service, you ("Customer") agree to these Terms. If you do not agree, do not use the Service.

## 1. Definitions

- **"Service"** means the Clinical Co-Pilot sidecar, the OpenEMR module (`oe-module-clinical-copilot`), and any associated documentation, updates, or supporting infrastructure.
- **"PHI"** means Protected Health Information as defined under the Health Insurance Portability and Accountability Act (HIPAA).
- **"Clinician User"** means a licensed healthcare professional accessing the Service through Customer's OpenEMR install.
- **"Subscription Plan"** means the plan tier (Starter, Pro, Enterprise) selected by Customer.

## 2. License grant

Subject to Customer's compliance with these Terms and payment of all fees, we grant Customer a non-exclusive, non-transferable, revocable license to use the Service for the duration of the Subscription Plan. The OpenEMR PHP module source is separately licensed under GPL-3.0-or-later; nothing in these Terms restricts the GPL grant.

## 3. Customer responsibilities

Customer represents and warrants that:

a. Customer is a covered entity, business associate, or otherwise authorized to provide PHI to a business associate under HIPAA, and Customer's use of the Service complies with HIPAA.

b. Customer has obtained all necessary consents and authorizations from individuals whose PHI is processed by the Service.

c. Each Clinician User is a licensed healthcare professional acting within the scope of their license.

d. Customer will not use the Service to make autonomous medical decisions without the supervision of a Clinician User. The Service is decision-support; it does not diagnose, prescribe, or treat.

e. Customer will not attempt to reverse engineer, modify in a manner that bypasses the license check, or use the Service to compete with us.

## 4. Acceptable use

Customer will not:

a. Upload malware, scan our infrastructure, or attempt to gain unauthorized access.
b. Use the Service to generate content that violates any applicable law or harms a third party.
c. Use the Service to process PHI for any patient who has not consented (where consent is required by applicable law).

## 5. Business Associate Agreement

A separate Business Associate Agreement (BAA) governs our handling of PHI. See `BAA_TEMPLATE.md`. The BAA is incorporated by reference. To the extent these Terms conflict with the BAA, the BAA controls.

## 6. Fees and payment

a. **Fees.** Customer will pay the fees set forth in the Subscription Plan selected at signup, billed monthly in advance via Stripe.

b. **Trial.** New customers receive a 14-day free trial. The trial ends automatically; no card is charged unless the customer adds payment and converts.

c. **Late payment.** Subscriptions in `past_due` for 14 days are suspended. License checks against `/chat` will return HTTP 402.

d. **Refunds.** Pre-paid annual subscriptions are pro-rated on cancellation. Monthly plans are non-refundable for the current period.

## 7. Term, termination

a. **Term.** These Terms begin on the date Customer first uses the Service and continue until terminated.

b. **Termination by Customer.** Customer may terminate at any time from the Stripe Customer Portal. Service continues through the end of the current billing period.

c. **Termination by us.** We may terminate or suspend immediately for material breach (including but not limited to non-payment, breach of section 3 or section 4) with notice.

d. **Survival.** Sections 6 (Fees), 8 (Disclaimer), 9 (Limitation of Liability), 10 (Indemnification), and 12 (Governing Law) survive termination. The BAA's data destruction obligations also survive.

## 8. Disclaimer

THE SERVICE IS PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. WE DO NOT WARRANT THAT THE SERVICE WILL BE UNINTERRUPTED, ERROR-FREE, OR MEET CUSTOMER'S REQUIREMENTS. WE DO NOT WARRANT ANY MEDICAL OUTCOME. THE SERVICE IS DECISION-SUPPORT ONLY. CLINICIAN USERS REMAIN RESPONSIBLE FOR ALL CLINICAL DECISIONS.

## 9. Limitation of liability

TO THE MAXIMUM EXTENT PERMITTED BY LAW, OUR AGGREGATE LIABILITY FOR ALL CLAIMS ARISING OUT OF OR RELATED TO THESE TERMS WILL NOT EXCEED THE AMOUNTS PAID BY CUSTOMER TO US IN THE TWELVE (12) MONTHS PRECEDING THE CLAIM. IN NO EVENT WILL WE BE LIABLE FOR INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES, INCLUDING LOST PROFITS OR LOST DATA, EVEN IF ADVISED OF THE POSSIBILITY.

## 10. Indemnification

Customer will indemnify, defend, and hold us harmless from third-party claims arising out of (a) Customer's use of the Service in violation of these Terms or applicable law, (b) Customer's content provided to the Service, or (c) Customer's breach of section 3.

## 11. Privacy

Our handling of personal data is described in the Privacy Policy. Our handling of PHI is governed by the BAA.

## 12. Governing law and dispute resolution

These Terms are governed by the laws of {GOVERNING_JURISDICTION} (United States) without regard to its conflict-of-laws rules. Any dispute will be resolved by binding arbitration in {ARBITRATION_VENUE} under the rules of the American Arbitration Association. The prevailing party may recover reasonable attorneys' fees.

## 13. Miscellaneous

a. **Entire agreement.** These Terms, the BAA, and the Subscription Plan order form constitute the entire agreement.

b. **Modification.** We may modify these Terms; we will provide notice at least thirty (30) days in advance for material changes.

c. **Notices.** Notices to us: relays.inanity.0n@icloud.com. Notices to Customer: the email address on file at signup.

d. **No waiver.** Failure to enforce any provision is not a waiver.

e. **Severability.** If any provision is held unenforceable, the remaining provisions remain in effect.
