//! Catálogo normalizado, estado local y motor de sync.

pub mod item;
pub mod state;
pub mod sync;

pub use item::CatalogItem;
pub use state::State;
pub use sync::{SyncEngine, SyncReport};
