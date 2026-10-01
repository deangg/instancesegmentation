# Post-Harvest Fruit Defect Segmentation

Two-stage YOLO26 instance segmentation of visible surface defects on apple and tomato.
CIPHER | Artificial Intelligence 2 | AM3 | Mapúa University

Members: Ralph Kevin G. Morales, Alexander Jann P. Espia, Deangelo James P. Largueza, Niel Francis M. Arligue
Adviser: Dr. Lysa V. Comia

## What it does

Stage 1 segments the whole fruit. Stage 2 starts from the Stage 1 weights and segments three visible defect classes:

| Class | Visible appearance |
|---|---|
| `bruise_discoloration` | Smooth dark or discolored patch |
| `rot_mold_decay` | Soft collapsed or moldy tissue |
| `surface_damage` | Cut crack scar or dry spot |

Masks describe appearance only. They are not a disease diagnosis.

## Results (apple)

| Model | Split | Mask P | Mask R | mAP50 | mAP50-95 |
|---|---|---:|---:|---:|---:|
| Stage 1 Run 21 (YOLO26l) | Validation | 1.000 | 0.986 | 0.985 | 0.985 |
| Stage 2 Run 22 (YOLO26l) | Validation | see `runs/final_metrics.json` | | | |
| Stage 2 Run 22 (YOLO26l) | Test | see `runs/final_metrics.json` | | | |

All 20 runs from the first pilot to the final model are in `runs/run_history.csv`.

## Run the app locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Put the checkpoints in `models/`:

| File | Source |
|---|---|
| `apple_stage1.pt` | `MyDrive/YOLOv26/runs/apple-stage1-whole-oct01/run21_*/weights/best.pt` |
| `apple_stage2.pt` | `MyDrive/YOLOv26/runs/apple-stage2-defects-oct01/run22_*/weights/best.pt` |
| `tomato_stage1.pt` | Tomato Stage 1 `best.pt` |
| `tomato_stage2.pt` | Tomato Stage 2 `best.pt` |

The app accepts JPG PNG WEBP and BMP uploads or built-in samples. It shows colored masks with a legend, each
defect's share of the image and of the fruit, inference time and a PNG download. A missing model or an unreadable
file gives a clear message instead of an error.

## Repository layout

```text
streamlit_app.py     web app
src/inference.py     prediction, overlay and coverage helpers used by the app
notebooks/           training and evaluation notebook (Colab, outputs kept)
runs/                run history, final metrics and plots for Runs 20 to 22
paper/               IEEE paper (.docx)
slides/              final defense deck (.pptx)
reports/figures/     charts and example images
app/samples/         demo images from the test split
models/              checkpoints (not in git when larger than GitHub allows)
```

## Data

The dataset is in the submission folder next to this repository (`dataset/apple-sep30-fruit9`).

| Split | Images | Capture groups |
|---|---:|---:|
| Train | 2,110 | 726 |
| Validation | 215 | 210 |
| Test | 79 | 75 |
| Reserve (never used) | 63 | 61 |

Sources: AFruitDB grading photos (Mojumdar et al. 2025, Data in Brief), the Lab2Wild apple rot set by S. Nesteruk
on Kaggle (CC BY-NC-SA 4.0) and photos taken by the team. AFruitDB grade folders are image-level labels and were not
used as targets. Every pixel mask was drawn and reviewed by the team. Derived masks from Lab2Wild images follow
CC BY-NC-SA 4.0: non-commercial use with attribution and the same license.

## Training

Training ran in Google Colab on one NVIDIA A100 80 GB with Ultralytics 8.4.126. See the notebook for every setting.
The two-stage idea is adapted from Leiva et al. (2026), Plant Methods 22, 29.

## AI use

Claude (Anthropic) and OpenAI Codex were used for planning, explanation, drafting and code scaffolding. The team
reviewed and tested all outputs. Full transcripts are in the submission under `ai_usage/`.
