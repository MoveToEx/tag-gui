# Tagger

An image tagging app based on PySide6, intended to assist in small-scale image dataset processing, specifically LoRA datasets.

## Install

[uv](https://docs.astral.sh/uv/) is required

```sh
$ git clone https://github.com/MoveToEx/tagger.git
$ cd tagger
$ uv sync --all-groups    # if AI tagging is needed
$ uv sync                 # if AI tagging is not needed
$ uv run python ./main.py
```

You may want to alter the CUDA accelerator to use in the pyproject.toml:

```toml
[[tool.uv.index]]
name = "pytorch-cu130"
url = "https://download.pytorch.org/whl/cu130"
explicit = true

[tool.uv.sources]
torch = [
    { index = "pytorch-cu130" },
]
torchvision = [
    { index = "pytorch-cu130" },
]
```

After modifying, use `uv lock` to re-lock the dependencies, and use `uv sync --all-groups` to sync the venv with the lockfile.

## Usage

Start by opening a directory containing images of supported format (PNG/JPG/WEBP) and tags (should be named `{image_stem}.txt` or `{image_full}.txt`, ignored when conflicting). Supports drag & drop. 

> [!NOTE]
> It is assumed that users are training caption-based LoRAs and will randomly shuffle tags during training. Therefore, this app does not keep the tag order and stores tags in a `set[str]`. It might not work well if you're training an order-aware model.

#### Add/Delete traversal

_Tags_ > _Add Tags_/_Delete Tags_ allows you to add tags to or delete tags from open folders and decide on a one-by-one basis.   

Adding/Deleting tags will ask you for the image to act on and a list of tags. When traversing, you need to decide on which one to add or delete for each image-tag pair. Tags already present/absent will be ignored. 

Decisions are committed only when you click on finish button, and are saved to memory whenever it is changed (i.e. whenever you toggle a tag).  
You can use <kbd>Space</kbd> to toggle a tag, <kbd>↑</kbd>/<kbd>↓</kbd> to select between tags, <kbd>←</kbd>/<kbd>→</kbd>/<kbd>Enter</kbd> to navigate between images (this is purely navigational and does not affect in-memory decision store), and <kbd>A</kbd> to select/deselect all tags.

#### Complex filter

_Tags_ > _Complex Filter_ allows you to write custom Python scripts to filter out images with complex conditions.  

The script contains a `check(fn: str, tags: set[str]) -> bool` function, which accepts the file name (relative to the open folder) and the tag list of an image. Images that the function returns `True` will be collected into a result table. Double clicking on one row will focus the image in the main window.

#### Bulk operation

_Tags_ > _Bulk Operation_ allows you to programmatically apply changes to images' tags.  

The script contains a `process(fn: str, tags: set[str]) -> set[str]` function which returns the new list of tags. After running all images through the script, the results are compared against the original ones to calculate the difference, and you need to review the differences image-by-image. 

#### Tag review

_Tags_ > _Review Tags_ allows you to thoroughly review tags within a folder and determine whether they should be deleted one-by-one.  

Decisions are stored in memory and are committed only when the revision finishes.  

#### AI tagging

_Tags_ > _AI Tagging_ allows you to tag images with AI models.  

This requires the `ai-tagger` dependency group to be installed. If not, the menu item will be disabled.  

To use AI tagging, select and download models in the settings window.  
Local models come from the system-wide huggingface cache folder, so you can also download models using hf-cli outside of the app.  

After selecting images and inference parameters the app will start inference. The heavy dependencies used here are imported lazily to speed up startup, so preparation step might take longer.  

After inference completes, you are supposed to check tags one by one. You can use shortcuts from the traversal window here.

## Tests

```powershell
uv run pytest
```

Qt tests run with the offscreen platform plugin and do not require a visible
desktop session.
