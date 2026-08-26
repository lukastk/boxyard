#!/bin/sh
# Resolve the 44 META conflicts on macbook (2026-08-26).
#
# Every one of them has the SAME shape, verified box by box before this was
# written: macbook added exactly one group (`archived` x38, `dormant` x6) and
# changed nothing else; the remote gained `write_owner` and changed nothing
# else; parents are identical. So neither side has anything the other lacks
# beyond those two edits, and the merge is unambiguous — take the remote's
# boxmeta, then re-add the group.
#
# Order matters. The force PULL takes the remote's META (bringing
# `write_owner`, which is what macbook is missing) and drops macbook's group;
# `add-to-group` then puts the group back and pushes. Doing it the other way
# round would push macbook's owner-less boxmeta over the fleet's.
#
# macbook is NOT the write_owner of these boxes, which is fine: ownership
# gates CONF and DATA, and the gate is decided AFTER the META sync
# (cmds/03_sync_box.pct.py, `# ---- Write ownership ----`). A non-owner may
# push META.
#
# RUN IT ON MACBOOK:  ssh-target macbook 'sh -s' < this-file
set -e

resolve() {
  box=$1
  group=$2
  echo "== $box  (+$group)"
  boxyard sync -r "$box" --sync-direction pull --sync-setting force --sync-choices meta
  boxyard add-to-group -r "$box" "$group" --sync-after
}

resolve 20260224_4hqyly__trading-bot archived
resolve 20260517_71qq35__find-chinese-research-on-the-west archived
resolve 20260519_7zpsxj__adu-portfolio archived
resolve 20260522_0mzygp__tbi-investigation dormant
resolve 20260602_h8b30h__benanav-steered-catallaxy-research archived
resolve 20260608_08z3f8__mysetup-review archived
resolve 20260611_ylau3x__scuttlebug-ui-mcp archived
resolve 20260619_u83pi8__netrun archived
resolve 20260622_0fsldt__scuttlebug-design-bible dormant
resolve 20260622_g78ulq__adu-design-bible archived
resolve 20260622_oo996d__ADI-website dormant
resolve 20260622_rxyhoo__care-visa-sponsorship-database dormant
resolve 20260626_radkar__trading-agent-alpaca archived
resolve 20260626_rnae51__choice-under-value-pluralism dormant
resolve 20260628_mhccur__auto-hegel-test archived
resolve 20260629_124vtd__choice-under-value-pluralism-proof-glm dormant
resolve 20260629_nsi3g3__ITUC-corporate-underminers-2 archived
resolve 20260701_epb6vr__hetzner-abuse-report archived
resolve 20260702_x58bny__pelican-pen-test archived
resolve 20260703_3fhb3i__pentest-sunlight-square archived
resolve 20260708_mvan9h__gesamtkunstwerk archived
resolve 20260710_baswj1__corkboard archived
resolve 20260710_yuj1am__corkboard-ui-mockups archived
resolve 20260726_evxbnm__dagnet-db archived
resolve 20260726_v97etu__dagnet archived
resolve 20260731_kpeh5m__corkboard-template-test-drive archived
resolve 20260731_n6lpf6__corkboard-investigation-template archived
resolve 20260801_9b6xzk__corkboard-supervision-regression-3 archived
resolve 20260801_bcsbi8__corkboard-source-workflow-test-drive-2 archived
resolve 20260802_nuaipg__ituc-corporate-underminers-2026 archived
resolve 20260803_1rt8tb__ituc-corporate-underminers-2026__hatvp archived
resolve 20260803_2i53yk__ituc-corporate-underminers-2026__pairwise-search archived
resolve 20260803_bytnlq__ituc-corporate-underminers-2026__ted archived
resolve 20260803_fgxim5__ituc-corporate-underminers-2026__eutr archived
resolve 20260803_kqm78g__ituc-corporate-underminers-2026__form5500 archived
resolve 20260803_lnjsnc__ituc-corporate-underminers-2026__opensanctions archived
resolve 20260803_nhxi3u__ituc-corporate-underminers-2026__icij archived
resolve 20260803_wp0odc__ituc-corporate-underminers-2026__uk-contracts archived
resolve 20260803_xnt1h9__ituc-corporate-underminers-2026__bhrrc archived
resolve 20260805_77y9rp__ituc-corporate-underminers-2026__violation-tracker archived
resolve 20260805_82a3ja__ituc-corporate-underminers-2026__sec-edgar archived
resolve 20260806_zw16gg__ituc-corporate-underminers-2026__obsidian archived
resolve 20260810_smhq1s__subswitcher archived
resolve 20260818_7glmx1__russian-doll-summariser archived

echo
echo "Now re-run: boxyard doctor"
