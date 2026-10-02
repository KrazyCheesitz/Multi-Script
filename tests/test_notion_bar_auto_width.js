const fs=require('fs');
const notion=fs.readFileSync('extension/providers/notion.js','utf8');
const main=fs.readFileSync('extension/core/main.js','utf8');
const css=fs.readFileSync('extension/overlay.css','utf8');
function ok(v,m){if(!v)throw new Error(m)}
ok(notion.includes('const barAnchor = composerFrame'),'Notion bar does not anchor to exact composer card');
ok(!/function barMount\(\)[\s\S]{0,180}parent: box\.parentElement/.test(notion),'old wide sibling mount remains');
ok(/bar\.style\.width = Math\.round\(r\.width\) \+ "px"/.test(main),'anchored bar does not copy measured composer width');
ok(main.includes('function syncBarFit(width)')&&main.includes('syncBarFit(r.width)'),'measured-width responsive mode missing');
ok(css.includes('#zs-bar.zs-bar-narrow #zs-discord')&&css.includes('#zs-bar.zs-bar-compact #zs-switch-name'),'narrow composer controls do not collapse');
ok(/width: 100%/.test(css)&&css.includes('#zs-bar.zs-bar-anchored'),'base inline/anchored width styles missing');
console.log('PASS Notion bar auto-width: exact composer anchor, live resize tracking, and narrow-control compaction');
