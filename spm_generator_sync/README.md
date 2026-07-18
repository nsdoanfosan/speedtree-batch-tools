# SPM Generator Sync

같은 수종의 SpeedTree SPM을 `마스터 → 자식` 제작 계보로 관리하는 세 번째 독립 도구입니다.

```text
weed_black_locast
├─ ◆ SK_tree_black_locast_01.spm  [MASTER]
│  ├─ ↳ SK_tree_black_locast_02.spm [FOLLOWER]
│  └─ ↳ SK_tree_black_locast_03.spm [FOLLOWER]
└─ ○ SK_tree_black_locast_04.spm  [INDEPENDENT]
```

`SPM_Generator_Sync.bat`을 더블클릭해 실행합니다.

## 무엇을 동기화하는가

- 마스터 Base 아래 공통 Generator 속성과 곡선
- Base 이름·GUID·Base filter는 읽기만 하며 절대 변경하지 않음
- BaseRef 표시 이름만 Base 기반의 고유한 export-safe 형식으로 정리
  - 예: `Ref_Leaf_3_001`, `Ref_BranchBig_2_001`
  - 영문·숫자·밑줄만 사용하고 전체 Generator 이름과 충돌하면 다음 번호 사용
  - BaseRef의 계층 순서에 따라 같은 Base 안에서 번호 부여
- 마스터에만 있는 `추가 예정` Generator 구조
- 자식에 대응 Base 자체가 없으면 해당 Base와 하위 구조를 자식 `Tree` 아래에 새로 연결
- 새 Generator의 Material/Mesh 참조는 마스터의 숫자 ID를 그대로 복사하지 않고, 에셋 이름을
  기준으로 자식 SPM의 로컬 ID로 변환
- 필요한 머티리얼이 자식 SPM에 없으면 머티리얼 정의와 연결된 Cutout/Supplemental Mesh를
  충돌 없는 새 로컬 ID로 함께 복사한 뒤 새 Generator에 연결
- 과거 동기화에서 Leaf 속성이 cluster 재질처럼 다른 역할의 에셋을 가리키게 된 참조도 자동 교정
- Leaf / Branch / End 역할별 아이콘 배경색
  - Leaf: 녹색
  - Branch: 파란색
  - End: 빨간색
- 같은 Base를 사용하는 BaseRef에도 같은 색 적용
- `BranchBig`/`BranchSmall`처럼 같은 계열의 서로 다른 Base는 같은 색조 안에서 밝기를 다르게 표시
- 자식 전용 Generator는 더 짙은 배경과 밝은 전경 아이콘으로 표시

기본적으로 다음 항목은 자식의 값을 보존합니다.

- Generator 이름과 GUID
- Random Seed
- 재질, 메시, Collection/asset 참조
- BaseRef의 배치/Generation 설정
- 기존 자식 Generator의 `Generation > Pass`
- Node/Freehand Edit (`Nodes` XML은 수정하지 않음)
- 자식에만 있는 `자식 전용` Generator 구조

`Pass`는 자식별 Reference/Base 계산 순서에 속하므로 마스터 값으로 덮어쓰지 않습니다. 동기화 후에는
SpeedTree 규칙에 따라 일반 계층의 `Parent pass <= Child pass`와
`Reference pass < 참조 Base pass`를 정적으로 검사합니다. 기존 값을 내리지 않고 필요한 값만 올리며,
Base 아래의 재사용 템플릿 subtree에는 Base pass를 전파하지 않습니다. Base filter가 비어 있으면 모든
Base를 대상으로 보고, `|`, `&`, `!`, `()`, `*`, `?`, 따옴표, `=`, `==` 검색 문법도 해석합니다.

기존 자식 Generator가 정상적인 같은 역할의 다른 재질을 사용하는 경우에는 해당 변형을 보존합니다.
로컬 ID가 없거나 `Leaf → cluster`처럼 역할이 충돌하는 경우에만 마스터의 에셋 이름을 자식 에셋
테이블에서 다시 찾아 안전하게 교정합니다. 이름이 같은 에셋도 없으면 마스터의 에셋 정의를 복사하며,
미리보기 상세에는 `에셋 복사` 항목으로 머티리얼과 메시 이름 및 새 ID를 표시합니다.

마스터와 자식의 같은 부모 아래에서 Generator Type별 순서를 기준으로 공통 노드를 대응시킵니다.
마스터에 더 많은 노드가 있으면 `추가 예정`으로 표시하고 다음 동기화에서 자식에 추가합니다.
이는 아직 동기화하지 않았거나 마스터를 나중에 수정했을 때 생깁니다. 자식에 더 많은 노드가
있으면 `자식 전용`으로 표시하고 삭제하지 않습니다. 표에는 개수를 간단히 표시하고, 자식 행을
선택하면 Base·Generator 이름·타입·경로를 모두 확인할 수 있습니다.

## 사용 순서

1. `나무 루트`를 선택하고 `폴더 다시 검사`를 누릅니다.
   - 기본 `SK_ SPM만 보기`를 끄면 폴더 안의 모든 SPM을 표시합니다.
   - SPM 행을 선택하고 `Ctrl+C` 또는 `선택 경로 복사`를 누르면 `D:\...\파일.spm`
     전체 경로가 클립보드에 복사되어 Everything 검색창에 바로 붙여넣을 수 있습니다.
   - 여러 행은 한 줄에 하나씩 복사되며, 폴더 행은 폴더 전체 경로를 복사합니다.
2. 자식으로 만들 SPM 행을 마스터 SPM 행 위로 드래그해 놓습니다.
   - 마스터 후보·미지정·독립 행 위에 놓으면 그 행이 마스터로 확정됩니다.
   - 이미 연결된 자식을 다른 마스터 위에 놓으면 새 마스터로 재연결됩니다.
   - 여러 행을 선택한 뒤 함께 드래그할 수도 있습니다.
   - 기존 `선택을 마스터로`, `선택을 자식으로 연결` 버튼도 보조 방식으로 유지됩니다.
   - 마스터로 확정되는 즉시 자식 유무와 관계없이 Base 분류, BaseRef 이름, 아이콘 색상이 SPM에 적용됩니다.
     승격 전 원본과 기존 관계 설정은 `_spm_backups/master_promotion_*`에 백업됩니다.
3. 자식 Base별로 따라갈 마스터 Base를 확인합니다.
   - Base 이름은 변경되지 않으므로 여러 자식 Base가 같은 마스터 Base 제작 규칙을 따라갈 수 있습니다.
   - 구조가 다른 두 Branch를 별도로 관리하려면 마스터도 `BranchBig`, `BranchSmall`처럼 Base를 분리하거나 하나를 `독립 Base`로 둡니다.
   - 동기화하지 않을 Base는 `독립 Base`로 둡니다.
4. 마스터의 `Base 색 분류`를 확인합니다.
5. `변경 미리보기`로 공통/추가 예정/자식 전용/색상 변경을 확인합니다.
6. 선택 자식 또는 마스터의 모든 자식을 동기화합니다.
   - 하단 진행 표시줄에서 현재 파일과 `패치 계산 → XML 검사 → SpeedTree 사전검사 → 백업 → 저장`
     단계를 확인할 수 있습니다.
   - 실제 SpeedTree 계산 중에도 경과 시간이 계속 갱신되므로 작업이 멈춘 것인지 기다리는 중인지
     구분할 수 있습니다.

파일명의 `_01`은 화면에서 `MASTER 후보`로만 제안합니다. 이름만으로 관계를 확정하거나 파일을
수정하지 않습니다. 확정한 관계는 나무 폴더의 `spm_generator_sync.json`에 상대 파일명으로 저장됩니다.

## 안전 규칙

- 미리보기는 읽기 전용입니다.
- 적용 전에 모든 패치를 메모리에서 먼저 만들고 XML/Generator/Link/BaseRef 무결성을 검사합니다.
- SpeedTree 명령행 export가 성공해도 Generator 오류가 남을 수 있으므로 Pass 의존성은 별도의 정적
  검사로 저장 전에 차단합니다.
- 마스터와 자식의 `Tree > Shape:Radius`를 비교합니다.
  - 자식이 1.5배 이상 크면 주의로 표시합니다.
  - 2.5배 이상 크면 거리 기반 생성의 노드·폴리곤 폭증 위험으로 원본 쓰기 전에 차단합니다.
  - 큰 나무는 작은 나무 마스터에 연결하지 않고 큰 나무용 별도 마스터를 사용합니다.
- 마스터와 선택한 모든 자식은 한 트랜잭션으로 처리합니다.
- `SpeedTree 10.1 실제 검증`은 같은 폴더의 임시 복사본을 먼저 계산·XML export합니다.
  실패하거나 5분을 넘기면 임시 파일만 제거하고 원본은 수정하지 않습니다.
- 사전검사가 성공한 뒤 첫 저장 전에 `<나무 폴더>\_spm_backups\generator_sync_날짜시간\`에 모두 백업합니다.
- 저장·무결성 검사·SpeedTree 검증 중 하나라도 실패하면 모든 파일을 백업으로 복구합니다.

공식 근거:

- [Reference generator 설정](https://docs.unity3d.com/speedtree-modeler/manual/add-and-set-up-a-reference-generator.html)
- [Generation properties의 Pass 규칙](https://docs.unity3d.com/speedtree-modeler/manual/generation-properties.html)
- [Base filter 검색 문법](https://docs.unity3d.com/speedtree-modeler/manual/search-syntax.html)

## 성능

- 폴더 보드는 SPM 수정 시간과 크기를 키로 Generator/Link 분석·구조 비교·동기화 해시를 캐시합니다.
- 결과는 PC 로컬 `spm_generator_sync_cache.json`에 저장되어 파일이 바뀌지 않으면 반복 새로고침뿐
  아니라 프로그램을 닫았다 다시 열어도 기존 분석을 즉시 재사용합니다.
- 속성 해시는 XML 깊은 복사 없이 정규화된 구조를 사용합니다.

## 설정 파일

- 수종 관계: 각 나무 폴더의 `spm_generator_sync.json`
- PC별 경로와 마지막 루트: `spm_generator_sync_config.json` (Git 제외)

## SK Batch 연동 준비

엔진은 패키지 진입점을 제공하므로, 안정화 후 기존 SK Batch의 `0. Generator Sync` 단계에서
같은 트랜잭션 함수를 그대로 호출할 수 있습니다. 별도의 SPM 수정 로직을 복제하지 않습니다.

```python
from spm_generator_sync import apply_group_transaction

result = apply_group_transaction(
    tree_folder,
    master_name,
    selected_followers,
    speedtree_exe=speedtree_exe,
    xml_ini=xml_ini,
)
```

## CLI

```powershell
python .\spm_generator_sync\spm_generator_sync.py scan "D:\OneDrive\Forestportfolio\02_nature\Tree" --sk-only
python .\spm_generator_sync\spm_generator_sync.py inspect "...\tree_01.spm"
python .\spm_generator_sync\spm_generator_sync.py repair-passes "...\tree_02.spm" --speedtree-exe "...\SpeedTree_Modeler.exe" --xml-ini "...\Options_HI_Xml.ini"
python .\spm_generator_sync\spm_generator_sync.py preview-auto "...\weed_black_locast" SK_tree_black_locast_01.spm SK_tree_black_locast_02.spm SK_tree_black_locast_03.spm
python .\spm_generator_sync\spm_generator_sync.py preview "...\weed_black_locast" SK_tree_black_locast_01.spm SK_tree_black_locast_02.spm
python .\spm_generator_sync\spm_generator_sync.py verify-auto-copy "...\weed_black_locast" SK_tree_black_locast_01.spm SK_tree_black_locast_02.spm --speedtree-exe "...\SpeedTree_Modeler.exe" --xml-ini "...\Options_HI_Xml.ini"
```

## 테스트

```powershell
python -m unittest discover -s .\spm_generator_sync\tests -v
python -m py_compile .\spm_generator_sync\spm_generator_sync.py .\spm_generator_sync\spm_generator_sync_gui.pyw
```
