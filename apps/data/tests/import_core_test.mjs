// Tests the "new registry from a file" core as SHIPPED: the block between the IMPORT CORE markers in dist/index.html.
// Run: node apps/data/tests/import_core_test.mjs
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const html=readFileSync(new URL('../dist/index.html',import.meta.url),'utf8');
const a=html.indexOf('// ==== IMPORT CORE'),b=html.indexOf('// ==== IMPORT CORE END ====');
assert.ok(a>=0&&b>a,'import core block not found');
const C=new Function(html.slice(a,b)+';return {parseDelimited,tableFromGrid,tableFromJSON,fieldNamesFor,parseNum,parseDate,parseDateTime,dateOrder,inferColumn,inferTable,buildRecords,schemaDefinition,personalReason,normPhone};')();
let n=0;const t=(name,fn)=>{fn();n++;};

const CSV=`Work ID,Ward,Ward Name,Work description,Sanctioned (Rs),Start date,Completed,Engineer email,Latitude,Longitude,PIN,Status
W-001,12,Shivajinagar,"Drain desilting, phase 1","1,48,000",03/04/2025,yes,ae.ward12@example.org,12.9857,77.6050,560001,Done
W-002,12,Shivajinagar,Footpath repair,"2,10,500",15/04/2025,no,ae.ward12@example.org,12.9861,77.6044,560001,Ongoing
W-003,13,Bharathinagar,Streetlight,48000,22/05/2025,no,ae.ward13@example.org,12.9902,77.6101,560005,Ongoing
W-004,13,Bharathinagar,Road patching,"3,05,250",01/06/2025,yes,ae.ward13@example.org,12.9911,77.6093,560005,Done
W-005,14,Jayamahal,Drain,75000,10/06/2025,no,ae.ward14@example.org,13.0003,77.6012,560046,Ongoing
W-006,14,Jayamahal,Park fence,"1,20,000",30/06/2025,no,ae.ward14@example.org,13.0010,77.6020,560046,Ongoing
W-007,15,Vasanthnagar,Desilting,two lakh,05/07/2025,no,ae.ward15@example.org,12.9930,77.5940,560052,Done
W-008,15,Vasanthnagar,Bus shelter,"95,000",12/07/2025,yes,ae.ward15@example.org,12.9925,77.5951,560052,Done
W-008,15,Vasanthnagar,Bus shelter (dup),"95,000",12/07/2025,yes,ae.ward15@example.org,12.9925,77.5951,560052,Done
`;
t('guesses each column, tolerating one bad value',()=>{
  const g=C.inferTable(C.tableFromGrid(C.parseDelimited(CSV),true));
  const ty=Object.fromEntries(g.cols.map(c=>[c.name,c.type]));
  assert.deepEqual(ty,{work_id:'text',ward:'integer',ward_name:'text',work_description:'text',sanctioned_rs:'integer',start_date:'date',
    completed:'boolean',engineer_email:'email',latitude:'latitude',longitude:'longitude',pin:'pincode',status:'choice'});
  assert.equal(g.key,'work_id');
  assert.equal(g.cols.find(c=>c.name==='engineer_email').personal,'email address');});
t('converts rows, lists the ones that do not fit, numbers them as file lines',()=>{
  const tb=C.tableFromGrid(C.parseDelimited(CSV),true),g=C.inferTable(tb);
  const r=C.buildRecords(tb,g.cols,{schema:'dataset.ward_works',tenant:'janaagraha',key:g.key,lineOffset:1});
  assert.equal(r.recs.length,7);assert.equal(r.badRows,2);
  assert.deepEqual(r.problems.map(p=>[p.row,p.msg]),[[8,'not a whole number'],[10,'repeats row 9 (the key must be unique)']]);
  const o=r.recs[0].obj;
  assert.equal(o.id,'ds:janaagraha:dataset/ward_works/W-001');assert.equal(o.sanctioned_rs,148000);assert.equal(o.start_date,'2025-04-03');
  assert.equal(o.completed,true);assert.equal(o.tenant,'janaagraha');});
t('schema: required fields, types, personal markers, no extra fields',()=>{
  const tb=C.tableFromGrid(C.parseDelimited(CSV),true),g=C.inferTable(tb);
  const d=C.schemaDefinition(g.cols,{schema:'dataset.ward_works',title:'Ward works',kind:'record',sensitivity:'internal',key:g.key});
  assert.equal(d.additionalProperties,false);assert.equal(d['x-kind'],'record');
  assert.ok(d.required.includes('work_id'));assert.equal(d.properties.start_date.format,'date');
  assert.deepEqual(d.properties.status.enum,['Done','Ongoing']);assert.equal(d.properties.engineer_email['x-personal'],true);});
t('dates, numbers, phones',()=>{
  assert.equal(C.parseDate('03/04/2025','dmy'),'2025-04-03');assert.equal(C.parseDate('31/02/2025','dmy'),null);
  assert.equal(C.parseDate('12 Mar 2024'),'2024-03-12');assert.equal(C.parseDate('Mar 12, 2024'),'2024-03-12');
  assert.equal(C.parseDateTime('03/04/2025 2:05 pm','dmy'),'2025-04-03T14:05:00+05:30');
  assert.deepEqual(C.dateOrder(['01/13/2025','02/02/2025']),{order:'mdy',assumed:false});
  assert.equal(C.parseNum('₹ 1,48,41,881'),14841881);assert.equal(C.parseNum('(500)'),-500);assert.equal(C.parseNum('1,2'),null);
  assert.equal(C.normPhone('+91 98450-12345'),'9845012345');assert.equal(C.normPhone('12345'),null);
  assert.equal(C.inferColumn('Code',['007','012','100']).type,'text');});
t('JSON: wrapped rows, flattening, GeoJSON points',()=>{
  const j=C.tableFromJSON({data:[{a:1,b:{c:'x',d:[1,2]}},{a:2,e:true}]});
  assert.deepEqual(j.headers,['a','b.c','b.d','e']);assert.deepEqual(j.rows[0],['1','x','[1,2]','']);
  const g=C.tableFromJSON({type:'FeatureCollection',features:[{properties:{name:'Lake'},geometry:{type:'Point',coordinates:[77.6,12.9]}}]});
  assert.deepEqual(g.headers,['name','longitude','latitude']);});
t('field names avoid the record envelope and repeats',()=>{
  assert.deepEqual(C.fieldNamesFor(['ID','Schema','Name','Name','2024 total','']),['source_id','schema_value','name','name_2','f_2024_total','column_6']);});
t('personal-data hints',()=>{
  for(const [h,want] of [['Applicant Name',true],['Ward Name',false],['Bank Name',false],['Aadhaar No',true],['DOB',true],['Contractor',false]])
    assert.equal(!!C.personalReason(h,'text'),want,h);});
console.log(`import core: ${n} tests passed`);
