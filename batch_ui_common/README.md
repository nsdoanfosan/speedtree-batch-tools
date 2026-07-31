# Batch UI Common

SpeedTree 배치 GUI들이 같은 행 선택·경로 복사 경험을 공유하기 위한 작은 공통
패키지입니다. BAT 실행기는 그대로 분리하되, 편의 기능의 동작과 회귀 테스트는 이
패키지에서 한 번만 관리합니다.

## 사용자 동작 계약

- 목록을 처음 열거나 `전체 선택`을 누른 직후에는 모든 실행 대상이 활성화됩니다.
- 이 상태에서 첫 행을 클릭하면 그 행만 활성화되고 나머지는 비활성화됩니다.
- 이후 클릭은 개별 행을 추가로 켜거나 끕니다. `전체 선택`을 누르면 첫 클릭 단독
  활성화가 다시 준비됩니다.
- 경로 복사는 실행용 체크 상태가 아니라 Treeview에서 실제 선택한 행을 대상으로
  합니다.
- `Ctrl+C`와 화면의 경로 복사 버튼은 같은 API를 호출합니다.
- Everything에 바로 붙여넣을 수 있도록 단일 파일은 절대경로로 복사하고, 여러
  파일은 각 절대경로를 따옴표로 감싼 OR 검색식으로 복사합니다. Windows 기준
  대소문자 중복은 제거합니다.
- 선택 행에 복사 가능한 경로가 없으면 기존 클립보드를 지우지 않습니다.

## 도구별 적용

| GUI | 실행 대상 활성화 | 첫 클릭 단독 활성화 | 경로 복사 |
|---|---|---|---|
| PCG ST9 Texture Batch | 폴더/SPM 계층 체크 (폴더=자식 전체, SPM=개별) | 적용 | 선택한 SK/원본 SPM |
| SK Vegetation Batch | SPM 행 체크 | 적용 | 선택한 SPM 파일 |
| SPM Generator Sync | 기본 다중 선택 유지 | 해당 없음 | 선택한 SPM 또는 폴더 |

SPM Generator Sync는 관계 편집을 위해 `Ctrl`/`Shift` 다중 선택이 필요하므로 체크
행 컨트롤러를 적용하지 않습니다. 경로 복사 규칙만 공통 API를 사용합니다.

## 공개 API

- `CheckedRowController`: 전체 활성 상태의 첫 클릭 단독화와 이후 개별 토글
- `clipboard_text`: 절대경로 변환, 중복 제거, Everything용 텍스트 생성
- `copy_paths_to_clipboard`: Tk 호환 클립보드 반영
- `selected_row_paths`, `copy_selected_row_paths`: Treeview 선택과 앱별 경로 어댑터 연결

새 편의 기능은 `batch_ui_common/tests`에 공통 동작 테스트를 추가하고, 영향을 받는
각 GUI에 얇은 통합 테스트를 추가한 뒤 연결합니다.
