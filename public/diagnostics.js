const status=document.querySelector('#status'),results=document.querySelector('#results');
let context;
try{
 const id=new URLSearchParams(location.search).get('track');
 const response=await fetch('/api/library');if(!response.ok)throw new Error('Library unavailable');
 const library=await response.json(),track=library.tracks.find(t=>t.id===id);
 if(!track)throw new Error('Choose a saved track using ?track=TRACK_ID.');
 context=new AudioContext();
 const audioResponse=await fetch(track.audioUrl);if(!audioResponse.ok)throw new Error('Saved audio missing');
 const decoded=await context.decodeAudioData(await audioResponse.arrayBuffer());
 const start=track.kind==='loop'?track.loopStart:0;
 const end=track.kind==='loop'?(track.loopEnd??decoded.duration):decoded.duration;
 const first=Math.round(start*decoded.sampleRate),last=Math.round(end*decoded.sampleRate);
 if(first<0||last<=first||last>decoded.length)throw new Error('Saved loop bounds exceed the decoded audio.');
 const buffer=context.createBuffer(decoded.numberOfChannels,last-first,decoded.sampleRate);
 for(let c=0;c<buffer.numberOfChannels;c++)buffer.copyToChannel(decoded.getChannelData(c).subarray(first,last),c);
 const side=Math.min(.5,buffer.duration/4),sr=decoded.sampleRate;
 const offline=new OfflineAudioContext(buffer.numberOfChannels,Math.round(side*2*sr),sr);
 const source=offline.createBufferSource();source.buffer=buffer;source.loop=true;source.connect(offline.destination);source.start(0,buffer.duration-side);
 const rendered=await offline.startRendering();let peak=0,jump=0,maxNeighborStep=0,minRMS=Infinity,finite=true;
 const boundary=Math.round(side*sr),windowSize=Math.max(1,Math.round(.01*sr));
 for(let c=0;c<rendered.numberOfChannels;c++){
  const a=rendered.getChannelData(c);jump=Math.max(jump,Math.abs(a[boundary]-a[boundary-1]));
  for(let i=0;i<a.length;i++){finite&&=Number.isFinite(a[i]);peak=Math.max(peak,Math.abs(a[i]));if(i>0&&i!==boundary)maxNeighborStep=Math.max(maxNeighborStep,Math.abs(a[i]-a[i-1]));}
  for(let n=0;n<a.length;n+=windowSize){let sum=0,count=Math.min(windowSize,a.length-n);for(let i=n;i<n+count;i++)sum+=a[i]*a[i];minRMS=Math.min(minRMS,Math.sqrt(sum/count));}
 }
 const report={track:track.title,artist:track.artist,kind:track.kind,savedBounds:[start,end],decodedSeconds:decoded.duration,playableSeconds:buffer.duration,finiteSamples:finite,peakAtWrap:peak,wrapSampleDelta:jump,largestNeighboringSampleDelta:maxNeighborStep,minimum10msRMS:minRMS,renderedSeconds:rendered.duration,clippingAtWrap:peak>=1};
 const success=finite&&peak<1;
 status.textContent=success?'PASS — audio decodes and the wrap renders within range.':'CHECK — audio rendered with non-finite samples or clipping.';
 results.textContent=JSON.stringify(report,null,2);document.body.dataset.result=success?'pass':'check';
}catch(error){status.textContent='FAILED — '+error.message;results.textContent=String(error);document.body.dataset.result='failed';}
finally{if(context)await context.close();}
