# Requirements register (release 1.1.0)

Checked-in list of the BRD v2.0 requirements this release is traced against. `python3 scripts/test.py trace`
reads it by default, and `python3 scripts/test.py trace --check` (CI, blocking) fails when an in-scope
requirement has no test case referencing it in a case catalogue or a suite script (static check; results
are not needed).

Status values:

| Status | Meaning | Needs a test case |
|---|---|---|
| Fixed | In this release, built as the BRD states | yes |
| Decision | In this release, built as decided in an ADR / sign-off (note says which) | yes |
| Change request | Scope changed by an approved change request (note gives the reference) | yes |
| Out of release | Not delivered in this release (deferred or out of scope); cases may record the absence | no |

Changing a row from an in-scope status to "Out of release" needs the product owner's sign-off; record it in the
Notes column and in CHANGELOG.md. Source: BRD v2.0 (Outreach 360 CRM), extracted 2026-10-18.

| ID | Priority | Requirement | BRD section | Status | Notes |
|---|---|---|---|---|---|
| BR-DF-01 | - | Campaigns stall at about 330 contacts | 5.1 Existing System Defect Fixes (Phase 1) | Fixed |  |
| BR-DF-02 | - | Enrollment fails after upload | 5.1 Existing System Defect Fixes (Phase 1) | Fixed |  |
| BR-DF-03 | - | Adding contacts from the Prospects tab fails | 5.1 Existing System Defect Fixes (Phase 1) | Fixed |  |
| BR-DF-04 | - | Delivery events dropped; status per email not reliable | 5.1 Existing System Defect Fixes (Phase 1) | Fixed |  |
| BR-DF-05 | - | Replies and out-of-office not visible; Microsoft 365 blocks password login | 5.1 Existing System Defect Fixes (Phase 1) | Fixed |  |
| BR-DF-06 | - | Same email can be sent twice | 5.1 Existing System Defect Fixes (Phase 1) | Fixed |  |
| BR-DF-07 | - | Domain health and auto-pause not working | 5.1 Existing System Defect Fixes (Phase 1) | Fixed |  |
| BR-DF-08 | - | Global unsubscribe not enforced across campaigns | 5.1 Existing System Defect Fixes (Phase 1) | Fixed |  |
| BR-DF-09 | - | Security gaps: default login secret, unverified email webhook, plain-text mailbox passwords, rate limiting off | 5.1 Existing System Defect Fixes (Phase 1) | Fixed |  |
| BR-CM-01 | M | Contact record with standard properties: name, email, phone, mobile, job title, LinkedIn URL, location, time zone, lead source, owner, create date, last activity and last contacted dates. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-02 | M | Email is the unique key; each contact has a system Record ID. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-03 | M | Company record with name, unique domain, industry, employees, annual revenue, phone, address and owner. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-04 | M | Companies are created from the email domain and contacts are associated automatically. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-05 | S | A contact can be linked to several companies (one primary), with association labels. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-06 | M | Lifecycle stage on contacts and companies: Subscriber, Lead, MQL, SQL, Opportunity, Customer, Evangelist, Other. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-07 | M | Lead status: New, Open, In progress, Attempted to contact, Connected, Open deal, Bad timing, Unqualified. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-08 | S | Lifecycle stage moves forward only, unless an admin changes it. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-09 | M | Admins create custom properties (text, number, date, dropdown, multi-checkbox, radio, phone, URL) without code changes. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-10 | S | Properties are organised in groups; selected properties are required on create. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-11 | M | Every property change is kept with old value, new value, user, time and source. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-12 | M | Three-column record page: key properties, activity timeline, associated records and lists. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-13 | M | Users add notes and log calls, emails and meetings from the record. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-14 | M | The timeline shows notes, calls, meetings, tasks, campaign emails (sent, opened, clicked, replied) and property changes, filterable by type and user. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-15 | S | Tasks with due date, reminder, owner and priority, and a "My tasks" queue. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-16 | C | @mention a colleague in a note. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-17 | M | Table views of contacts and companies with column chooser, sort and paging. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-18 | M | Advanced filters with AND / OR conditions on any property. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-19 | M | Saved views (All, My contacts, Unassigned, Recently created), personal or shared. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-20 | S | Board view by lifecycle stage or lead status, with drag and drop. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-21 | M | Global search by name, email, phone, company or domain. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-22 | M | Active lists update membership automatically from filter criteria. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-23 | M | Static lists hold a fixed set of records, added manually or by import. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-24 | M | Any list can be used as a campaign audience. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-25 | M | Import CSV / XLSX of contacts, companies, or both in one file with associations. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-26 | M | Map columns to properties, creating new properties during import if needed. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-27 | M | Create new records and update existing ones, matched by email or company domain. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-28 | M | Several users can import at the same time without creating duplicates. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-29 | M | Keep an import history with a downloadable error file per import. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-30 | S | Set owner and add to a static list during import. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-31 | S | Export any view to CSV / XLSX (admin and manager only). | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-32 | M | Detect duplicates (same email; similar name and company) and list them for review. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-33 | M | Merge two records, choosing the value to keep per property; timelines and associations are combined. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-34 | S | Flag invalid emails, phone formats and name capitalisation. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-35 | S | Restore deleted records within 90 days. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-36 | M | Contact and company owner, with bulk reassignment. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-37 | M | Bulk edit, assign owner, add to list, enroll in campaign and delete. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-38 | M | Admin and User roles in Phase 1; 4-level hierarchy visibility in Phase 2 (section 5.4). | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-39 | M | Email subscription status per contact, synced with the global unsubscribe list. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-40 | S | Consent and legal basis for processing recorded per contact (GDPR, DPDP Act). | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-41 | M | Market Research flow: research → create or import contacts → assign owners → add to lists → campaign. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-CM-42 | C | Enrich company data from the domain (industry, size, location). | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Out of release | Paid data enrichment is out of scope (BRD 3.3); RP-070/CM-082 record the absence |
| BR-CM-43 | C | Score contacts on fit and engagement. | 5.2 Contact Management -- modelled on HubSpot CRM (Phase 1) | Fixed |  |
| BR-SH-01 | M | Configure the 4-level hierarchy: L1 CEO / COO / Sales Head, L2 Business Development, L3 Business Executive, L4 Market Research team. | 5.4 Sales Hierarchy -- Role-Based Access (Phase 2) | Fixed |  |
| BR-SH-02 | M | Link each user to a manager; users see their own records and those of everyone below them. | 5.4 Sales Hierarchy -- Role-Based Access (Phase 2) | Fixed |  |
| BR-OV-01 | M | Allow up to 500 new contacts per user per day; follow-up emails do not count toward the limit. | 5.5 Daily New-Contact Limit (Phase 2) | Fixed |  |
| BR-LD-01 | M | Leads management: lead record with source, status and owner, linked to its contact. | 5.6 Leads & SQL Management (Phase 2) | Fixed |  |
| BR-LD-02 | M | Lead stages classification and management, aligned with the contact lifecycle stage. | 5.6 Leads & SQL Management (Phase 2) | Fixed |  |
| BR-LD-03 | M | SQL management: qualification criteria and an SQL queue. | 5.6 Leads & SQL Management (Phase 2) | Fixed |  |
| BR-LD-04 | M | Qualified leads tracking by age, owner and next step. | 5.6 Leads & SQL Management (Phase 2) | Fixed |  |
| BR-LD-05 | M | Leads pipeline board by stage. | 5.6 Leads & SQL Management (Phase 2) | Fixed |  |
| BR-LD-06 | M | SQL to sales stage conversion: convert an SQL to an opportunity, linked to contact and company. | 5.6 Leads & SQL Management (Phase 2) | Fixed |  |
| BR-SP-01 | M | Sales stages classification and management, with a probability per stage. | 5.7 Sales Pipeline Management (Phase 2) | Fixed |  |
| BR-SP-02 | M | Proposal pipeline: draft, sent, under review, accepted, rejected. | 5.7 Sales Pipeline Management (Phase 2) | Fixed |  |
| BR-SP-03 | M | Revenue pipeline tracking, weighted and unweighted. | 5.7 Sales Pipeline Management (Phase 2) | Fixed |  |
| BR-SP-04 | M | Both pipelines cover new and existing clients, with a client-type tag on each deal. | 5.7 Sales Pipeline Management (Phase 2) | Fixed |  |
| BR-DR-01 | M | Role dashboards for Sales Head, Business Development and Business Executive. | 5.8 Dashboards & Reports (Phase 2) | Fixed |  |
| BR-DR-02 | M | Reports on leads, SQLs and pipeline (campaign reports already exist under BRD v1.0). | 5.8 Dashboards & Reports (Phase 2) | Fixed |  |
| BR-DR-03 | M | Funnel from sent → replied → lead → SQL → opportunity → won. | 5.8 Dashboards & Reports (Phase 2) | Fixed |  |
| BR-FC-01 | M | Revenue forecast quarter-wise. | 5.9 Revenue Forecasting (Phase 2) | Fixed |  |
| BR-FC-02 | M | Revenue forecast financial-year-wise. | 5.9 Revenue Forecasting (Phase 2) | Fixed |  |
| BR-TP-01 | M | Inside sales team performance management: scorecard per rep with activities, leads, SQLs and deals won, and conversion rates by stage. | 5.10 Team Performance (Phase 2) | Fixed |  |
| BR-AI-01 | C | Optional AI-based contact discovery to find and enrich missing contact details. | 5.11 AI Contact Discovery (Phase 2, optional) | Out of release | AI contact discovery not built; RP-070 records the absence |
| BR-SF-01 | S | Convert a positive campaign reply to a lead in one click. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-02 | S | Record lead disqualification reasons and recycle leads. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-03 | S | Assign leads automatically (round robin or by region). | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-04 | S | Keep stage, amount and close-date history on opportunities. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-05 | S | Record win / loss reasons; alert owners on stale deals. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-06 | S | Show an activity timeline and tasks on opportunities. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-07 | S | Send in-app and email notifications. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-08 | S | Use forecast categories (Pipeline, Best case, Commit) and targets per rep. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-09 | S | Show target vs actual and a team leaderboard. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-10 | S | Provide an email template library with merge fields. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-11 | S | Link campaigns to the pipeline and revenue they produce (campaign ROI). | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-12 | S | Hide amount and revenue fields from roles that should not see them. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-13 | S | Workflow rules: on a field change, create a task, send an alert or update a field. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-14 | S | Let managers build custom reports. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-15 | S | Add products and quotes; generate a proposal PDF. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-16 | S | Sync calendar and email with Outlook / Gmail. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-17 | C | Route discounts above a threshold for approval. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-18 | C | A/B test email subject lines. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Fixed |  |
| BR-SF-19 | C | Single sign-on with Microsoft 365. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Out of release | Microsoft 365 single sign-on not built; RP-105 records the absence |
| BR-SF-20 | C | REST API and webhooks for other systems. | 5.12 Additional Recommended Features -- Salesforce-style (Phase 2) | Out of release | Outbound webhooks delivered (RP-106b); REST API keys for other systems deferred |
