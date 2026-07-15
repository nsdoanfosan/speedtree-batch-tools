# SpeedTree Batch Tools

SpeedTree 식생 변환 작업을 위한 독립형 Windows 배치 도구 모음입니다. Blender 애드온 본체와 분리해 GUI, 자동화 작업, 테스트를 이 저장소에서 관리합니다.

## 도구

- `SpeedTree_Batch_Tools.bat`: 아래 세 도구를 한 창의 탭으로 전환하는 통합 실행 파일 (`Ctrl+1/2/3` 지원, 현재 탭은 별도 창으로 분리 가능)
- `pcg_st9_texture_batch/PCG_ST9_Texture_Batch.bat`: PCG에서 사용하는 ST9 나무를 SK·나나이트·버추얼 텍스처 작업으로 연결하는 준비 보드
- `sk_batch/SK_Batch.bat`: SPM 수정, Blender 리페어, Unreal 전송을 단계별로 실행하는 SK 식생 배치 도구
- `spm_generator_sync/SPM_Generator_Sync.bat`: 같은 수종의 SPM을 마스터·자식·독립 계보로 관리하고 Base 제작 구조와 아이콘 색을 동기화하는 도구. 이후 SK Batch의 `0. Generator Sync`가 같은 엔진을 호출할 수 있는 패키지 진입점을 포함합니다.

각 도구의 상세 사용법은 해당 폴더의 `README.md`를 참고합니다.

## 공통 GUI 편의성

세 GUI의 행 활성화와 Everything용 경로 복사는 `batch_ui_common` 패키지를
단일 진실 공급원으로 사용합니다. 새 GUI 편의 기능은 각 `.pyw`에 복제하지 않고
공통 API와 테스트에 먼저 추가한 뒤, 도구별 행 데이터만 얇은 어댑터로 연결합니다.
세부 동작 계약과 적용 범위는 `batch_ui_common/README.md`에 정리되어 있습니다.

## 저장소 배치

기본 구성은 두 저장소를 같은 상위 폴더에 체크아웃하는 방식입니다.

```text
GitHub/
├─ speedtree-batch-tools/
└─ speedtree-bone-weight-repair-addon/
```

`sk_batch`는 형제 저장소의 SpeedTree 10.1 프리셋을 자동으로 찾습니다. 다른 위치에 둔 경우 `SPEEDTREE_BWR_ADDON_DIR` 환경 변수에 `speedtree_bone_weight_repair` 패키지 폴더 경로를 지정할 수 있습니다. 기존 `sk_batch_config.json`에 `fbx_ini`와 `xml_ini`가 저장되어 있으면 그 값이 우선 적용됩니다.

## 외부 의존성

- Python 3
- Blender 및 활성화된 `speedtree_bone_weight_repair` 애드온
- SpeedTree Modeler 10.1
- PCG 텍스처 작업용 Adobe Substance 3D Designer
- 아틀라스 생성 작업용 `atlas_leaf_mesh_builder` Blender 애드온
- Unreal 전송 작업용 Send to Unreal 및 프로젝트 측 동적 바람 임포트 기능

개인 PC 경로가 들어가는 설정 JSON, 실행 상태, 로그, 생성 리포트는 로컬에는 유지되지만 Git에는 포함되지 않습니다.

## 테스트

```powershell
python -m unittest discover -s .\tests -v
python -m unittest discover -s .\batch_ui_common\tests -v
python -m unittest discover -s .\pcg_st9_texture_batch\tests -v
python -m unittest discover -s .\sk_batch\tests -v
python -m unittest discover -s .\spm_generator_sync\tests -v
python -m compileall -q .\batch_ui_common .\pcg_st9_texture_batch .\sk_batch .\spm_generator_sync
```
