# Cermaq Media Watch POC

## Formål
Teste om vi kan erstatte Opoint med AI-drevet medieovervåkning
basert på FreshRSS + Claude API.

## Tidsfrist
6 uker fra opprettelse.

## Suksesskriterier
- Sender daglig mail i 14 dager uten manuell intervensjon
- Minst 3 kolleger åpner mailen 5+ ganger på 14 dager
- Klassifiseringskvalitet over 80 % på 50 stikkprøver
- Mindre enn 2 kr/dag i Claude API-kostnader
- Eier leser mailen først hver morgen

## Ut av scope
- Frontend (kommer i produksjonsversjon)
- Database (bare JSON-filer for POC)
- Multi-bruker-håndtering
- Finance watch (separat prosjekt senere)

## Stack
- Python pipeline
- FreshRSS for RSS-aggregering
- Claude API for klassifisering
- MJML for mail-templates
- Microsoft Graph for sending

## Deploy
Railway
