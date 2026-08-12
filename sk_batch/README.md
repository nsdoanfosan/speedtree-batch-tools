# SK Vegetation Batch (SPM → Blender → Unreal)

WPO/마스크 방식 식생을 스켈레탈 메시(SK_*.spm)로 교체하는 반복 작업을
단계별 버튼으로 자동화하는 도구. `SK_Batch.bat` 더블클릭으로 실행.

검사·SPM·Blender·Unreal·전체 자동 요청은 창 내부 FIFO와 동시에 프로세스 간
공용 FIFO에도 등록된다. 별도 PCG/SPM Generator Sync 창에서 먼저 등록한 작업이
있으면 `공용 대기열 대기 · 전체 N번째`로 표시하고, 해당 작업이 끝난 뒤에만
SpeedTree·Blender·Unreal 단계를 시작한다. 대기 중에도 다음 선택 작업을 로컬
대기열에 계속 추가할 수 있다.

GUI를 시작할 때는 에셋을 열지 않는 코드 컴파일 게이트가 먼저 약 1초 동안
저장소 Python 구문과 SK Batch 역할 계약을 검사한다. 일반 owner SPM을 Atlas
생산자나 본 보정 대상으로 잘못 넣는 변경, ② 결과를 무시하고 ③에서 전체 감사를
되풀이하는 변경, 런타임 에셋 컴파일 웨이브의 재도입은 창이 열리기 전에 차단되어
공용 오류 로그와 메시지로 표시된다. 개발 중에는
`python sk_batch\code_compile_gate.py`로 같은 검사를 직접 실행할 수 있다.

트리의 G(trunk/branch 감쇠)와 R(leaf 근접도)이 SpeedTree부터 Unreal까지 갖는
정확한 의미와 보존 규칙은 [Tree Vertex Color 계약](docs/tree_vertex_color_contract.md)을
기준으로 한다.

## 사용 순서 (빠른 것 → 느린 것)

**🔍 검사 (수정 없음)** — 표에 상태만 채운다:
SPM 본 세팅 상태(고정 본/자동 본/본 꺼짐/M_ 필요), blend 최신 여부,
push에 필요한 핸드오프 산출물(wind JSON) 준비 여부. SK Batch를 열 때도 저장된
과거 문구가 아니라 실제 SPM/blend 시간을 비교해 `생성 필요` 또는
`Blender 갱신 필요 — SPM이 더 최근에 수정됨`을 바로 표시한다.
`고정 본(Absolute)`은 미보정 오류가 아니라 가지마다 고정된 본 수를 쓰는
Generator이고, `자동 본(Relative)`은 가지 길이에 따라 본 밀도가 계산되는
Generator다. 따라서 ①을 완료한 뒤에도 의도적으로 고정 본 항목이 남을 수 있다.
`수동 본 유지` 항목의 총 본 수는 Generator 설정 개수로 추정하지 않는다. 검증된
SpeedTree XML export receipt의 실제 Bone 수를 표시하고, SPM SHA-256까지 같으면
현재 실측값, 이후 SPM 내용이 바뀌었으면 `최근 실측`과 `미재검증`을 함께 표시한다.
검증할 XML이 없을 때만 `본 수 미측정`으로 표시한다. 이 검사는 파일 저장,
timestamp 변경, SpeedTree 실행, 상태 캐시 저장을 하지 않으므로 ①~③ 재실행을
예약하지 않는다.

목록 표에는 ①~③ 상태를 짧게 줄여 표시한다. 행을 선택하면 표 아래 `선택 항목
상세`에서 전체 경로와 줄이지 않은 상태 문구를 볼 수 있으며, `Ctrl+C`로 선택한
SPM 경로를 복사할 수 있다.
실제 `Cluster` 폴더의 production output도 일반 식생과 같은
`SK_branch_elm_01.spm` 규격을 사용한다. 예전 무접두사
`branch_elm_01.spm`만 남은 경우에는 첫 ①/② 실행 때 내용을 검증한 뒤 canonical
`SK_` 이름으로 한 번 정규화한다. 이후 표·SpeedTree FBX/STMAT·Blend·보고서·Unreal
identity는 모두 canonical `SK_` stem만 사용하며, canonical 내용을 다시 무접두사
파일로 게시하지 않는다. 남아 있는 무접두사 파일은 레거시 입력 증거일 뿐 현재
output으로 판정하지 않는다.
SPM 안에 Atlas Leaf Mesh Builder 출력이 있으면 이름만 보지 않고 실제
`Leaf Mesh`/`Frond` Generator의 Material과 Mesh가 같은 새 Atlas 자산에 연결됐는지도
검사한다. 숨김·삭제·컬링 상태이거나 실제 생성 Node가 0개인 Generator는 export 대상이
아니므로 숫자와 재질 누락 판정에서 제외한다. 연결이 남은 경우에는 내부 용어 대신
`새 Atlas 연결 N개 / 기존 재질 사용 N개`로 표시하며, 실제 내보내는 참조가 끊긴
경우에만 별도의 연결 오류로 차단한다.
오래 걸리는 단계를 돌리기 전에 항상 먼저 눌러보는 용도.

**① SPM 본 세팅 (파일별 편차 있음, 기본 4개 동시 실행)** — SPM만 수정:
- **Cluster 구조 루트 본 정규화**: `Cluster` 폴더의 SPM도 ①을 건너뛰지 않는다.
  각 Tree 루트 아래 첫 번째 실제 렌더 메시를 찾고 그 구조 루트만 `Absolute/1`,
  나머지 Branch는 `Absolute/0`으로 만든다. 메시가 없는 배치용 pivot Branch는
  숨김 처리한다. 한 SPM에 사용 조각이 여러 개면 구조 루트와 축도 여러 개다.
  XML에서 각 사용 Cluster 조각마다 루트 본이 정확히 하나이고 descendant 본이
  없는지, FBX에 실제 메시 지오메트리가 남는지까지 검증한 뒤 저장한다.
  Blender Repair는 FBX의 짧은 `_End` marker를 축 본으로 쓰지 않는다. Raw XML의
  렌더 구조 루트 `Start→End`를 `Bone_N_Start` 본 하나로 재구성하고, 여러 조각은
  서로 parent가 없는 독립 축으로 유지한다. 일반 트리용 Base reparent는 Cluster에
  적용하지 않는다.
  실패·중단 시 일반 캘리브레이션과 같은 백업/marker 복구를 적용한다.
- **본 캘리브레이션 (총 본 수 예산 방식)**: SpeedTree의 Relative 본 스타일은
  spline 길이에 비례해 본을 넣으므로, 값이 낮으면 짧은 잔가지는 자동으로
  0본이 된다(원하는 동작). 문제는 "가지당 평균 N본"을 목표로 잡으면 가지가
  수만 개인 큰 나무가 폭증한다는 것(elm_03: 15,234가지 × 3 ≈ 45k목표 →
  80k본). 그래서 **총 본 수 예산**으로 하나의 Relative 값을 맞춘다:
  1. 프로브: 대상 제너레이터를 잠시 Absolute/1로 바꿔 XML 익스포트 —
     Absolute/1 = "가지당 본 정확히 1개"라서 총 본 수 = **총 가지 수**. 동시에
     각 probe bone의 길이를 읽어 실제 branch 길이 분포도 얻는다.
  2. 예산: `min(가지 수 × 가지당 목표, 최대 총 본 수)`
     · 작은 식물: 가지가 적어 상한 안 걸림 → 가지당 목표대로
     · 큰 나무: 상한이 걸리면 **Base 소속 자동 대상부터 모두 Absolute/0**으로
       내리고 Tree/트렁크 쪽 밀도를 먼저 보존
  3. Base를 꺼도 Tree 대상만으로 상한을 넘을 때에만 Tree 대상의 Relative 밀도를
     낮춘다. Base를 끈 뒤 상한 안이면 Tree는 원래 가지당 목표 밀도를 유지한다.
     Tree 전용 Absolute/1 재프로브의 길이 합으로 Relative 값을 추정하고 XML을
     검증한다. 특이한 곡선/구조로 목표 범위를 벗어난 파일은 수동 처리로 넘긴다.
  - **GUID 계층 대상 선정**: Branch 이름이 아니라 Generator/Node GUID를 따라
    `Tree → Branch`에서 시작하는 실제 root 체인을 자동 대상으로 잡는다. 이 체인의
    `Absolute+Bones=0`은 의도적 무본으로 넘기지 않고 자동 활성화한다.
  - **Base reference 처리**:
    · `tree`: Branch 역할 Base의 첫 Branch 단계까지만 포함하고 내부 단계는 제외
    · `bush/weed`: Branch 역할 Base의 첫 Branch와 그 자식 Branch까지 **2단계**만
      포함하고 더 아래 단계는 잔가지로 제외
    · Leaf 역할 Base가 `Classic+Any(Mode=0/Style=0)`이면 연결 Branch를 제외하고,
      Phyllotaxy 등 다른 배치는 `tree` 1단계, `bush/weed` 2단계까지만 대상으로 사용
    · weed/bush에서 최상위 Branch가 `Zone` 아래 생성되는 구조도 root 체인으로 인정
    이 단계 제한보다 더 줄이는 자산별 자동 예외는 만들지 않는다. 특정 자산에서 본이
    여전히 많으면 해당 행을 `수동 본 유지`로 바꾸고 SpeedTree에서 직접 설정한다.
    Base 역할은 `spm_generator_sync.json`의 명시적 분류 또는 저장된 역할 아이콘 색으로
    확인한다. 역할을 알 수 없는 Base는 이름으로 추측하지 않고 제외·경고하며, 그 결과
    자동 대상이 하나도 없거나 같은 Branch GUID가 Tree와 Base 양쪽에서 발견되면 오류 처리한다.
  - XML의 대상 Branch 노드는 많은데 Absolute/1 실제 프로브가 0~3가지만 내보내면
    잘못된 저본 성공으로 인정하지 않고 원본을 복원해 `수동 처리 필요`로 남긴다.
  - JSON 결과에는 자동 대상 제너레이터 수, Tree/Base 첫 단계/Base 내부 대상 수,
    Base 제외 수, 모호한 공유 GUID, 예상 Node 수, 실제 프로브 가지 수를 기록한다.
  - 제너레이터별 개별 값이 아니라 하나의 값이라 **중복 제너레이터 이름**
    (elm_03의 'Bifurcating'×3 등)에 안전하고, **Size scalar도 자동 반영**
    (Relative가 실제 spline 길이 비례)
  - 실측 예: 소형 풀 anamone 273본(가지당 2), 대형 elm_03 15,234가지 →
    80,000본에서 **1,776본**으로 (잔가지 0, Big 계열 큰 가지에 집중)
- 머티리얼 이름에 `M_` 프리픽스 (attr-only, FBX 전파 검증됨)
- 수정 전 `_spm_backups\`에 백업, 실패 시 자동 복원
- **동시 실행 및 느린 파일 건너뛰기**: 독립 SPM은 기본 4개씩 병렬 처리한다.
  SpeedTree 익스포트 1회가 120초를 넘으면 추가 계산을 기다리지 않고 원본을 복원해
  `수동 처리 필요`로 넘긴다. 정상 파일과 캐시 적중 파일은 계속 병렬 처리한다.
- **진행 피드백**: GUI 상단에는 전체 완료 파일 퍼센트와 실행 중인 작업 수만
  안정적으로 표시한다. 가지 프로브, Tree 전용 프로브, Relative 검증, FBX 검증 중
  어느 단계인지와 경과·남은 시간은 각 파일 행에서만 갱신해 병렬 작업끼리 표시가
  서로 튀지 않게 한다.
- **변경 없음 캐시**: 스캔할 때 SPM 콘텐츠 지문만 병렬 선계산한다
  (현재 74개 데이터 기준 약 0.02초). SPM 내용, 본 목표 옵션, SpeedTree/preset,
  `spm_audit.py`가 모두 같으면 ①은 Python/SpeedTree 프로세스를 띄우지 않고 즉시
  `✓ (변경 없음)`으로 끝난다. 관련 파일이나 옵션이 바뀌면 자동 무효화된다.
- **Absolute/1 프로브 캐시**: 옵션 변경이나 강제 재실행으로 다시 계산해야 해도
  topology와 대상 generator가 같으면 이전 branch count/length 프로브를 재사용한다.
  실제 `SK_weed_willow_02` 복사본에서 56.1초 → 33.9초로 줄었고 본 수(144)와
  Relative 값(4.1097)은 동일했다.
- **무변경 SPM 보존**: 검증을 실행했더라도 최종 XML이 시작 XML과 같으면 원래 gzip
  바이트와 타임스탬프를 복원한다. ①의 no-op 실행 때문에 최신 `.blend`가 오래된
  것으로 바뀌어 ②를 불필요하게 다시 도는 문제를 막는다.
- **문제 SPM 제한 보정**: 첫 Relative 검증이 목표 범위를 벗어나도 설정된 최대
  round 안에서 측정값을 bracket해 다시 보정한다. probe 길이가 없으면 렌더 대상의
  기존 공통 Relative 값을 우선 사용한다. root-only FBX가 armature-only이면
  Absolute/material 폴백을 검증하며, 끝까지 수렴하거나 렌더 geometry를 만들지
  못한 경우에만 원본을 복원하고 구체적인 실패 분류를 남긴다.
- **중지/시간 초과 정리**: 작업용 Python만 종료해 SpeedTree가 고아 프로세스로
  남지 않도록 Windows 프로세스 트리 전체를 종료한다. 진행 로그는 unbuffered로
  기록해 실행 중인 `.log`에서도 현재 프로브 단계를 확인할 수 있다.
- **수동 본 유지**: 각 SPM 행의 `본 모드`는 기본 `자동 계산 ▼`이다. 직접
  SpeedTree 본을 설정한 자산만 해당 행의 화살표를 눌러 `수동 본 유지`로 바꾼다.
  이 자산은 이후 ①을 다시 실행해도 단계 전체를 즉시 건너뛴다. SpeedTree 프로브를
  실행하지 않고 SPM에도 아무것도 쓰지 않는다. 체크된 여러 항목에 일괄 적용하지
  않으므로 다른 SPM을 실수로 잠그지 않는다.
  잠금 상태는 GUI 상태와 SPM 폴더의
  `_spm_backups/<SPM명>.skbatch_manual_bones.json`에 저장된다. 같은 드롭다운에서
  `자동 계산`을 선택하면 해제된다.

**② Blender Repair (느림, 기본 2개 동시 실행)** — 헤드리스 Blender로
BWR `SpeedTree → Import → Repair` 실행, wind 프리셋은 파일명 기반 자동
(tree/bush/weed·grass, deadleaves·deadbranches=무바람), SPM 옆에 같은 이름
`.blend` + wind JSON 저장. **이미 SPM보다 최신인 blend는 건너뛴다**
Cluster FBX에 양수 skin weight가 있으면 ①의 구조 루트 수에 대응하는 다중
`*_Start` deform root와 `*_End` 축 끝점을 그대로 보존해 Cluster Normalizer의
연결 컴포넌트 분할에 넘긴다. 단일 구조 루트 FBX가 armature와 축 본은 내보내지만
skin deformer를 완전히 생략한 경우에만, 실제 imported deform bone 하나를 보존하고
사용 메시 전체를 그 본에 weight 1.0으로 rigid 바인딩한다. 완전 무스킨 상태에서
본 수가 하나가 아니면 정상 다축 리그를 평탄화하지 않고 즉시 실패한다.
("완료된 항목도 다시 실행"으로 강제 가능). 재실행 = 갱신. 배치 Blender는
`--factory-startup`으로 시작하고 BWR만 명시적으로 켜므로 사용자용 애드온의 시작
오류와 등록 비용을 가져오지 않는다. SpeedTree가 만든 `.stmat`의 실제 Material
목록은 Blender를 띄우기 전에 가벼운 SpeedTree FBX 사전 export로 먼저 검사한다.
성공한 라이브 Repair 검사는 상태 파일에 `blend_resume_receipt`를 남긴다. 다음
비강제 실행은 이 영수증이 묶은 SPM, blend, Repair 보고서, wind JSON, stmat,
텍스처, 의존 SPM과 출력 설정이 모두 같을 때 완료 행을 worker/progress 대기열에
넣기 전에 제외한다. 핵심 SPM/blend/report/wind/stmat은 bounded content key까지
확인하고, 보고서가 열거한 나머지 계약 파일은 파일 ID·크기·mtime·ctime을 묶는다.
중단 후 다시 실행하면
미완료·실패·변경된 행만 남으며, 영수증이 없거나 하나라도 달라지면 기존의 엄격한
라이브 검증 경로로 돌아간다. 기존 상태 파일은 첫 라이브 상태 감사에서 현재 행의
영수증을 자동 생성한다.
이 검사를 통과한 항목만 무거운 Blender Repair로 넘어간다. 실제 생성 Node가 쓰는
재질이 FBX에서 빠졌다면 정확한 Generator 이름/GUID를 보고하고 차단하되 SPM은
수정하지 않는다.
SpeedTree 단일 export 슬롯 대기와 실제 FBX export 실행은 별도 시간 계약이다.
로그에는 `슬롯 대기 중`/`실행 중`이 구분되어 표시되며, 실제 900초 제한은 슬롯을
획득한 뒤부터 시작한다. 앞 작업을 기다린 시간 때문에 뒤 작업의 실행 시간이
줄어들지 않는다.
구형 `Cluster` 렌더 텍스처를 참조하는 Generator의 전경 자홍색 표시는 PCG ①에서
새 SK SPM을 생성한 직후에만 1회 기록한다. 기존 SK 데이터는
`migrate_legacy_cluster_markers.py --apply`로 1회 이관하며, SPM별 백업과 영구
receipt를 남긴다. 이후 판정은 공용 `speedtree_legacy_cluster_contract.py`가
receipt의 최초 Generator GUID를 읽어 담당한다. 현재 재질 경로나 표시색이 바뀌어도
계보는 유지되며, Generator Sync는 receipt 소유 GUID의 전경색을 덮어쓰지 않는다.
표시색만 있고 receipt가 없는 경우는 과거 문제 표시와 구분할 수 없으므로 자동
분류하지 않는다. 이후 재질 사전검사는 이 색을 변경하거나 원복하지 않는다.
Cluster Assembly 영수증이 현재 산출물 해시보다 오래된 경우에는 PCG 폴더 감사를
한 번 자동 재실행하고, 새 영수증이 실제 현재 해시로 검증된 뒤에만 Blender를
시작한다. 오래된 영수증을 무시하거나 완화해서 통과시키지는 않는다.

**③ Unreal Push** — 시작 전에 구조 준비 검사를 수행한다:
새 Atlas 재질과 Generator 연결, SpeedTree `.stmat` 재질 정체성, blend 존재+최신,
wind JSON 존재, 언리얼 에디터 실행 여부를 확인한다. 텍스처는 입장 조건이 아니다.
확정된 파일만 전달하고 부분·누락·불일치 후보는 비운 채 헤드리스 send2ue로 push한다
(임포트 시 머티리얼 파이프라인이 wind JSON 연결까지 자동 수행 + 디스크 저장).
headless transport의 Blender export도 기본 2개씩 처리하며 Unreal import는 한 세션에서
안전하게 순차 실행한다. Blender 오브젝트 이름에 충돌 방지 숫자가 붙더라도 wind
JSON은 오브젝트명이 아니라 선택한 SPM의 정규 이름으로 연결한다.

기본 transport는 `headless`다. Unreal Editor를 계속 열어 둔 채 export만 준비하려면
`unreal_wait`를 선택한다. 이 모드는 dependency 순서가 포함된 immutable manifest를
저장하고 행을 `export 완료 · Unreal 영구 대기`로 남기며 GUI를 재시작해도 유지된다.
Editor를 완전히 종료한 뒤 `대기 에셋 임포트` 버튼을 누르면 스캔 화면 밖을 포함한
영구 상태의 모든 대기 행을 다시 모아 원본·export fingerprint를 검증하고, 유효한
항목만 한 번의 `UnrealEditor-Cmd` headless 세션으로 임포트한다. 검증 중 Editor가 다시 실행되면
commandlet을 시작하지 않고 대기 상태를 보존한다.
대기 항목의 manifest와 export 파일은 작은 `unreal_wait_references.json` 영수증으로
artifact retention에 등록되며, import 완료 전에는 기간·용량 정리로 삭제되지 않는다.
같은 ①→②→③ 자동 실행에서는 ②가 이미 확정한 Repair·재질·Assembly 준비 결과를
job 내부 계약으로 ③에 직접 전달하므로 동일한 전체 감사를 다시 하지 않는다.
Unreal 실행 상태와 대화형 Blender의 미저장 변경은 ③ 직전에 계속 확인한다.
③만 단독으로 실행하거나 ③에서 새로 자동 포함된 항목은 ② 결과 계약이 없으므로
기존 전체 준비 검사를 그대로 수행한다.
Cluster Assembly가 있는 Tree만 선택해도 BWR manifest의 실제
`parts[].external_source.source_blend`를 따라 필요한 Cluster SPM을 자동 포함한다.
이때 이름이나 개수를 추측하지 않으며, Cluster source/export fingerprint가 최신이면
기존 export cache를 사용한다. Unreal import에서는 그 Cluster 자산이 현재 프로젝트에
실제로 존재하는지 확인하고, 모두 있으면 재임포트 없이 통과하며 누락된 경우에만
캐시된 export를 먼저 임포트한다. Cluster가 실패하면 그 Cluster를 요구하는 Tree만
`not_run`으로 남기고 독립 항목은 계속 처리한다.
단, 정규화된 Cluster 프로토타입이 생성 태그와 단일 `part_root` 계약을 만족하면
그 원본 리그용 JSON은 적용하지 않고 최종 Assembly 스켈레톤 재바인딩 단계까지
DynamicWind 적용을 유예한다. 일반 나무와 최종 Assembly의 wind JSON은 계속 필수다.

②/③ 실패 시 표와 저장 상태에는 `Unreal 연결 실패`, `메시를 찾지 못함`,
`add-on 로드 실패`, `FBX 메시 지오메트리 없음`, `시간 초과`처럼 짧은 원인을
남긴다. 전체 traceback과 상세 경로는 `sk_batch/logs/`의 JSON/log에 보존한다.

**🌙 목록 전체 자동 ①→②→③** — 체크 상태와 무관하게 현재 목록의 모든 항목에서
①을 모두 끝낸 뒤 ② 전체, 마지막으로 ③ 전체를 실행하는 야간 일괄 버튼이다. 개별
①/②/③ 버튼만 체크된 항목을 대상으로 한다. 개별 실패·수동 처리 항목은 상태와 로그에
남기고 다른 파일은 계속 진행한다. 중지 버튼은 현재 단계의 자식 프로세스까지 종료한다.

## 전체 실패 이력 자동 복구

`↻ 전체 실패 이력 재시도`는 체크 상태와 무관하게 current inventory 전체의 durable
실패 evidence를 다시 분류한다. 공식 reason code가 PCG texture 또는 Generator/Cluster
repair에 해당하면 즉시 최종 실패 목록에서 빼고 `자동 복구 대기 → PCG 텍스처 복구 중
→ Generator Sync 중 → Cluster 갱신 중 → 재검증 중 → Blender-Unreal 재시도 중`으로
표시한다. SK는 BAT를 하위 프로세스로 띄우지 않고 각 도구의 같은 exact job builder를
현재 shared queue lease 아래에서 직접 호출한다.

각 BAT 단계 직후 exact SPM을 fresh audit한다. 모든 repair와 audit를 통과한 항목만 기존
Blender/Send2UE/Unreal partition으로 복귀한다. 지원 불가 authoring/exporter 오류 또는
terminal repair/re-audit 실패만 최종 `실패`로 승격하며, 그때 파일명·친화적 원인·시도한
단계·남은 조치를 표시한다. 원래 raw reason code와 내부 오류는 state/receipt detail에만
보존한다. 취소된 항목은 최종 실패가 아니며 다음 전체 재시도에서 durable evidence로
재계획할 수 있다.

queue/phase 결과는 공용 terminal 계약이 정규화한 `completed`, `pending_unreal`,
`cancelled`, `failed`, `blocked`, `owner_lost` outcome만 소비한다. 따라서 성공·Unreal
대기·사용자 취소 행은 최종 실패 count/token/detail에 들어가지 않으며, 이 기능이 raw
phase 상태를 다시 해석하지 않는다.

## 옵션 설명 (GUI 툴팁과 동일)

- **가지당 목표 본 수** — 작은 식물의 목표. 총 본 수를 대략 `가지 수 × 이 값`으로
  맞춘다. Relative 값 자체는 파일마다 다르게 계산되는 것이 정상(길이 비례 계수).
- **최대 총 본 수** — 한 나무의 총 본 수 상한. 본 폭증 방지의 핵심. 큰 나무는
  Base 소속 본을 먼저 0으로 만들며, 그래도 초과할 때만 Tree/트렁크의 Relative
  밀도를 낮춘다.
- **우선순위 / CPU 코어** — 백그라운드 프로세스의 CPU 사용 제한.
  자식(SpeedTree CLI/Blender)에 상속. 헤드리스 Blender는 GPU를 쓰지 않는다.
- **완료된 항목도 다시 실행** — ①의 변경 없음 결과 캐시를 무시하고 다시 계산하며,
  ②에서도 최신 blend가 있어도 다시 만든다. ①의 Absolute/1 프로브 캐시는 안전한
  선계산 데이터이므로 강제 실행에서도 재사용된다.

## 주의

- 각 행의 Wind `현재값 ▼`을 눌러 자동(파일명 기준) 또는 TREE/BUSH/WEED/NONE을
  명시적으로 선택한다. 더블클릭 순환 방식은 사용하지 않는다.
- 이 값은 변경 불가능한 에셋 반응 프리셋 ID다. 수치 조정은 Unreal의
  `Project Settings > MyProject2 > Dynamic Wind Response Presets`에서 프리셋별로 한 번만 하며,
  레벨/날씨 Wind Source의 속도·진폭·방향 설정과는 별개다. `NONE`도 같은
  조정 창에 표시되고 기본 수치만 0이다.
- 백업은 각 폴더의 `_spm_backups\` 하위 폴더에만 쌓인다 (작업 폴더 오염 없음)
- 기존 UE 에셋을 다시 push할 때 .uasset이 Perforce read-only면 임포트가
  조용히 실패한다 → 먼저 p4 edit로 체크아웃할 것
- 로그/리포트: `sk_batch/logs/` (저장소 루트 기준)

## 실패 복구 규칙

- Base/BaseRef 레코드는 SPM 전체 개수가 아니라 FBX에 실제 생긴 orphan `_Start` 본을
  기준으로 역방향 매칭한다. 본이 꺼진 generator의 Base 레코드는 제외한다.
- Base 연결과 독립 root branch가 섞인 파일은 Base로 확인된 root만 원래 가지에 붙이고,
  남는 top-level root만 true root에 연결한다.
- 요청한 `Bone_1_Start`가 없으면 실제 root 중 가장 앞선 본(예: `Bone_1_End`)을 true root로
  추론한다.
- SpeedTree가 Relative 본 설정 또는 외부 atlas cutout 참조 때문에 armature-only FBX를
  만들면, 임시 FBX의 `Vertices` 존재 여부로 감지한다. root-only generator는
  `Absolute/1`로 되돌리고, 외부 Frond cutout 참조만 가장 가까운 내장/기본 material로
  복구한다. 추가된 atlas material asset 자체는 삭제하지 않는다.
- `BranchMesh`처럼 SPM에 Branch 본 generator 자체가 없는 자산은 FBX를 no-bones
  preset으로 내보내고, Blender에서 전체 geometry를 `Bone_1_Start` 하나에 rigid
  skin한다. XML에 본이 없어도 group 0 dynamic-wind JSON을 생성한다.
- 보이는 Branch가 모두 `Absolute/0`이어도 GUID상 Tree root 또는 허용된 Base 체인이면
  자동 대상으로 활성화한다. Base 역할 미분류는 제외·경고하고, Tree/Base 공유 GUID나
  본 속성 누락처럼 안전한 대상을 확정할 수 없는 구조는 원본을 수정하지 않고 오류 처리하며
  GUID와 원인을 JSON/GUI에 기록한다.
- 같은 SPM의 FBX/XML은 SpeedTree 프로세스 두 개로 동시에 열지 않고 순서대로 export한다.
  서로 다른 SPM의 ① 캘리브레이션 병렬 처리는 그대로 유지한다.
- 실패 중 생성된 최신 `.blend`만 보고 완료로 건너뛰지 않는다. blend와 wind JSON이 모두
  존재하고 SPM보다 최신일 때만 ②를 건너뛴다.

### raw Cluster Blender 결과와 원본 검토 상태

- `Cluster\branch_elm_01.spm` 같은 무접두사 레거시 입력은 한 번
  `SK_branch_elm_01.spm`으로 정규화한다. 이후 SPM/FBX/STMAT/JSON/Blend는 모두
  `SK_branch_elm_01` stem을 사용한다.
- 원본에 선언된 텍스처 경로가 부분적이거나 전혀 없어도 ② Blender 생성과 ③ Unreal Push를 계속한다. 보고서의 texture availability는 진단 정보이며 `unreal_push_ready`를 바꾸지 않는다.
- 안전하게 확인된 역할만 연결한다. 누락 후보는 비워 두고, 모호하거나 작업 범위를 벗어난 후보는 선택하지 않는다. 이 상태는 `source_review`나 자동 PCG 복구를 만들지 않는다.
- 실제 FBX material slot 부재, 재질 ID/슬롯 모호성, 메시·버텍스 payload 오류 같은 비텍스처 구조 문제만 `source_review` 또는 `blocked`가 될 수 있다.
