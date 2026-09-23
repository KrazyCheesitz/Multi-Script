const fs=require('fs'),path=require('path');
const root=path.resolve(__dirname,'..');
const R=new Function(fs.readFileSync(path.join(root,'extension/core/tool-routing.js'),'utf8')+';return ZSToolRouting;')();
const tools=[{name:'get_state',server:'roblox'},{name:'unity/get_state',server:'unity'},{name:'godot/get_version',server:'godot'},{name:'ms_critic_review',server:'zeroscript'}];
let x=R.resolve('godot.get_version','',tools);if(!x.ok||x.name!=='godot/get_version')throw Error('unique bare routing');
x=R.resolve('get_state','unity',tools);if(!x.ok||x.name!=='unity/get_state')throw Error('server collision routing');
x=R.resolve('get_state','',tools);if(!x.ok||x.name!=='get_state')throw Error('exact primary routing');
x=R.resolve('get_state','godot',tools);if(x.ok||x.reason!=='server')throw Error('wrong-server rejection');
console.log('PASS exact engine tool routing: unique, collision, server hint and rejection');
